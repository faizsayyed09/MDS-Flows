from langflow.custom import Component
from langflow.io import Output, DataInput, FloatInput, IntInput
from langflow.schema import Data
import re
from collections import Counter
from typing import List, Dict

class DataQualityProcessorComponent(Component):
    display_name = "Data Quality Processor (Enhanced)"
    description = "Multi-stage validation: KB articles (quality filter only), Tickets (deduplication + scoring)"
    
    inputs = [
        DataInput(
            name="sanitized_data",
            display_name="Sanitized Data",
            info="Output from PHI Detector component"
        ),
        FloatInput(
            name="similarity_threshold",
            display_name="Similarity Threshold (Tickets Only)",
            value=0.85,
            info="For tickets: similarity above this = duplicate (0.0-1.0)"
        ),
        IntInput(
            name="min_content_length",
            display_name="Minimum Content Length",
            value=100,
            info="Minimum characters to consider valid"
        ),
        IntInput(
            name="edge_case_threshold",
            display_name="Edge Case Score Threshold (Tickets Only)",
            value=6,
            info="For tickets: keep only scores >= this value (1-10)"
        )
    ]
    
    outputs = [
        Output(display_name="Processed Data", name="output", method="process_data")
    ]
    
    def process_data(self) -> Data:
        """Multi-stage validation pipeline"""
        try:
            sanitized_data = self.sanitized_data
            self.log(f"Stage 1: Data extraction")
            
            if hasattr(sanitized_data, 'data'):
                data_dict = sanitized_data.data
            elif isinstance(sanitized_data, dict):
                data_dict = sanitized_data
            else:
                try:
                    data_dict = dict(sanitized_data)
                except:
                    return Data(data={"status": "error", "error": "Invalid input type"})
            
            sanitized_records = (
                data_dict.get('sanitized_records') or
                data_dict.get('records') or
                data_dict.get('data') or
                []
            )
            
            data_type = data_dict.get('data_type', 'UNKNOWN')
            primary_col = data_dict.get('primary_column')
            
            self.log(f"Found {len(sanitized_records)} {data_type} records")
            
            if not sanitized_records:
                return Data(data={"status": "error", "error": "No records to process"})
            
            if data_type == "KB_ARTICLES":
                self.log(f"KB Articles: quality filter only (NO deduplication)")
                return self._process_kb_articles(data_dict, sanitized_records, primary_col)
            elif data_type == "HISTORICAL_TICKETS":
                self.log(f"Tickets: deduplication + edge case scoring")
                return self._process_tickets(data_dict, sanitized_records)
            else:
                return Data(data={
                    "status": "warning",
                    "processed_records": sanitized_records,
                    "original_count": len(sanitized_records)
                })
        except Exception as e:
            self.status = f"Error: {str(e)}"
            return Data(data={"status": "error", "error": str(e)})
    
    def _process_kb_articles(self, data_dict, records, primary_col=None):
        """Quality filter only - NO deduplication"""
        if not primary_col:
            primary_col = "Article body"
        
        valid_records = [r for r in records if len(str(r.get(primary_col, ''))) >= self.min_content_length]
        
        result = Data(data={
            "status": "success",
            "processing_type": "QUALITY_FILTER",
            "data_type": "KB_ARTICLES",
            "original_count": data_dict.get('original_count', len(records)),
            "after_quality_filter": len(valid_records),
            "deduplication_applied": False,
            "processed_records": valid_records,
            "phi_report": data_dict.get('phi_report', {})
        })
        
        self.status = f"KB: {len(valid_records)} curated articles (NOT deduplicated)"
        return result
    
    def _process_tickets(self, data_dict, records):
        """Deduplicate THEN score tickets"""
        primary_col = "Work notes"
        
        valid_records = [r for r in records if len(str(r.get(primary_col, ''))) >= self.min_content_length]
        
        if not valid_records:
            return Data(data={"status": "error", "error": "No valid tickets"})
        
        duplicate_groups = []
        processed = set()
        
        for i, record_i in enumerate(valid_records):
            if i in processed:
                continue
            text_i = str(record_i.get(primary_col, ''))
            group = [i]
            
            for j, record_j in enumerate(valid_records):
                if j <= i or j in processed:
                    continue
                text_j = str(record_j.get(primary_col, ''))
                similarity = self._cosine_similarity(text_i, text_j)
                
                if similarity >= self.similarity_threshold:
                    group.append(j)
                    processed.add(j)
            
            if len(group) > 1:
                duplicate_groups.append(group)
                processed.update(group)
        
        kept_indices = set(range(len(valid_records)))
        discarded_indices = set()
        
        for group in duplicate_groups:
            lengths = [(idx, len(str(valid_records[idx].get(primary_col, '')))) for idx in group]
            best_idx = max(lengths, key=lambda x: x[1])[0]
            for idx in group:
                if idx != best_idx:
                    kept_indices.discard(idx)
                    discarded_indices.add(idx)
        
        deduplicated_records = [valid_records[i] for i in sorted(kept_indices)]
        
        scored_tickets = []
        for ticket in deduplicated_records:
            score = self._calculate_ticket_score(ticket)
            scored_ticket = ticket.copy()
            scored_ticket['__EDGE_CASE_SCORE__'] = score['total_score']
            scored_ticket['__TRAINING_VALUE__'] = score['value_tier']
            scored_tickets.append(scored_ticket)
        
        scored_tickets.sort(key=lambda x: x['__EDGE_CASE_SCORE__'], reverse=True)
        
        high_value = [t for t in scored_tickets if t['__EDGE_CASE_SCORE__'] >= self.edge_case_threshold]
        low_value = [t for t in scored_tickets if t['__EDGE_CASE_SCORE__'] < self.edge_case_threshold]
        
        result = Data(data={
            "status": "success",
            "processing_type": "DEDUPLICATION_AND_SCORING",
            "data_type": "HISTORICAL_TICKETS",
            "original_count": data_dict.get('original_count', len(records)),
            "after_deduplication": len(deduplicated_records),
            "duplicates_removed": len(discarded_indices),
            "after_edge_case_filtering": len(high_value),
            "low_value_discarded": len(low_value),
            "processed_records": high_value,
            "phi_report": data_dict.get('phi_report', {})
        })
        
        self.status = f"Tickets: {len(discarded_indices)} dupes removed, {len(high_value)} high-value kept"
        return result
    
    def _calculate_ticket_score(self, ticket):
        score = 5.0
        reasons = []
        work_notes = str(ticket.get('Work notes', '')).lower()
        
        if len(work_notes) > 500:
            score += 2
        if any(kw in work_notes for kw in ['integrate', 'sync', 'api']):
            score += 3
        if any(kw in work_notes for kw in ['escalat', 'tier 2', 'tier 3']):
            score += 2
        if 'workaround' in work_notes:
            score += 2
        if any(kw in work_notes for kw in ['password reset', 'unlock account']):
            score -= 4
        if 'duplicate' in work_notes:
            score -= 5
        if len(work_notes) < 50:
            score -= 2
        
        score = max(0, min(10, score))
        
        if score >= 8:
            value_tier = "HIGH"
        elif score >= 6:
            value_tier = "MEDIUM"
        else:
            value_tier = "LOW"
        
        return {'total_score': round(score, 1), 'value_tier': value_tier}
    
    def _cosine_similarity(self, text1, text2):
        words1 = re.findall(r'\w+', text1.lower())
        words2 = re.findall(r'\w+', text2.lower())
        counter1 = Counter(words1)
        counter2 = Counter(words2)
        all_words = set(counter1.keys()) | set(counter2.keys())
        vec1 = [counter1.get(word, 0) for word in all_words]
        vec2 = [counter2.get(word, 0) for word in all_words]
        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        magnitude1 = sum(a * a for a in vec1) ** 0.5
        magnitude2 = sum(b * b for b in vec2) ** 0.5
        if magnitude1 == 0 or magnitude2 == 0:
            return 0.0
        return dot_product / (magnitude1 * magnitude2)
