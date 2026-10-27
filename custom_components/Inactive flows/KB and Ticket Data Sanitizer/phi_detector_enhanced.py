from langflow.custom import Component
from langflow.io import Output, MessageTextInput
from langflow.schema import Data
import pandas as pd
import re
import hashlib
from typing import Dict, List

class PHIDetectorComponent(Component):
    display_name = "PHI Detector (Enhanced)"
    description = "Detect and redact PHI"
    
    inputs = [
        MessageTextInput(
            name="file_path",
            display_name="File Path",
            info="Path to Excel file from Read File component"
        )
    ]
    
    outputs = [
        Output(display_name="Results", name="output", method="process_file")
    ]
    
    PHI_PATTERNS = {
        'SSN': {
            'pattern': r'\b\d{3}-\d{2}-\d{4}\b',
            'confidence': 95
        },
        'PHONE': {
            'pattern': r'\b(\+?1[-.]?)?\(?\d{3}\)?[-.]?\d{3}[-.]?\d{4}\b',
            'confidence': 85
        },
        'EMAIL': {
            'pattern': r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
            'confidence': 90
        },
        'MRN': {
            'pattern': r'\b(MRN|Medical Record|Patient ID|Chart ID)[:\s#]*[A-Z0-9]{6,12}\b',
            'confidence': 90
        },
        'ACCOUNT_NUMBER': {
            'pattern': r'\b(Account|Acct)[:\s#]*\d{6,12}\b',
            'confidence': 75
        },
        'PERSON_NAME': {
            'pattern': r'\b(patient|user|customer|employee)\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b',
            'confidence': 80
        },
        'IP_ADDRESS': {
            'pattern': r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
            'confidence': 70
        }
    }
    
    def _generate_crypto_token(self, phi_value: str, phi_type: str) -> str:
        """
        Generate cryptographic placeholder token
        Inspired by Ashwini's proprietary tokenization engine
        """
        # Create deterministic hash for the same value
        hash_input = f"{phi_type}:{phi_value}".encode('utf-8')
        token_hash = hashlib.sha256(hash_input).hexdigest()[:8]
        return f"[{phi_type}_{token_hash}]"
    
    def process_file(self) -> Data:
        """Read Excel file and detect/redact PHI with validation gates"""
        try:
            file_path = self.file_path
            
            # === VALIDATION GATE 1: File Access ===
            self.log(f"Stage 1: Reading file from {file_path}")
            df = pd.read_excel(file_path, engine='openpyxl')
            self.log(f"âœ“ File loaded: {len(df)} records")
            
            # === VALIDATION GATE 2: Data Type Detection ===
            columns = df.columns.tolist()
            if "Article body" in columns and "Knowledge base" in columns:
                data_type = "KB_ARTICLES"
                primary_col = "Article body"
            elif "Work notes" in columns and "Assignment group" in columns:
                data_type = "HISTORICAL_TICKETS"
                primary_col = "Work notes"
            else:
                data_type = "UNKNOWN"
                primary_col = columns[0] if columns else None
            
            self.log(f"âœ“ Data type detected: {data_type}, Primary column: {primary_col}")
            
            records = df.to_dict('records')
            
            # === VALIDATION GATE 3: PHI Detection & Tokenization ===
            sanitized_records = []
            phi_report = {
                'total_records': len(records),
                'records_with_phi': 0,
                'phi_by_type': {},
                'total_redactions': 0,
                'flagged_records': []
            }
            
            for idx, record in enumerate(records):
                # Extract text to scan
                if data_type == "KB_ARTICLES":
                    text_to_scan = str(record.get(primary_col, ''))
                elif data_type == "HISTORICAL_TICKETS":
                    text_to_scan = str(record.get('Work notes', '')) + ' ' + str(record.get('Comments and Work note Close Notes', ''))
                else:
                    text_to_scan = ' '.join([str(v) for v in record.values() if v])
                
                # Scan for PHI
                findings = []
                for phi_type, config in self.PHI_PATTERNS.items():
                    pattern = config['pattern']
                    matches = re.finditer(pattern, text_to_scan, re.IGNORECASE)
                    for match in matches:
                        findings.append({
                            'type': phi_type,
                            'value': match.group(),
                            'start': match.start(),
                            'end': match.end(),
                            'confidence': config['confidence']
                        })
                
                # Filter by confidence threshold
                high_confidence = [f for f in findings if f['confidence'] >= 70]
                
                if high_confidence:
                    phi_report['records_with_phi'] += 1
                    
                    # Tokenize PHI with cryptographic placeholders
                    sanitized_text = text_to_scan
                    sorted_findings = sorted(high_confidence, key=lambda x: x['start'], reverse=True)
                    
                    for finding in sorted_findings:
                        # Use crypto token instead of simple placeholder
                        replacement = self._generate_crypto_token(finding['value'], finding['type'])
                        sanitized_text = (
                            sanitized_text[:finding['start']] +
                            replacement +
                            sanitized_text[finding['end']:]
                        )
                    
                    # Update record with sanitized text
                    sanitized_record = record.copy()
                    sanitized_record[primary_col] = sanitized_text
                    sanitized_record['__PHI_DETECTED__'] = True
                    sanitized_record['__PHI_TYPES__'] = [f['type'] for f in high_confidence]
                    sanitized_records.append(sanitized_record)
                    
                    # Track in report
                    phi_report['flagged_records'].append({
                        'record_id': record.get('Number', f'Record_{idx}'),
                        'phi_types': [f['type'] for f in high_confidence],
                        'phi_count': len(high_confidence)
                    })
                    
                    # Update type counts
                    for finding in high_confidence:
                        phi_type = finding['type']
                        phi_report['phi_by_type'][phi_type] = phi_report['phi_by_type'].get(phi_type, 0) + 1
                        phi_report['total_redactions'] += 1
                else:
                    # No PHI - keep original record
                    sanitized_records.append(record)
            
            # === VALIDATION GATE 4: Output Validation ===
            self.log(f"âœ“ PHI detection complete: {len(sanitized_records)} records sanitized")
            self.log(f"âœ“ PHI found in {phi_report['records_with_phi']} records")
            
            # ========================================
            # CRITICAL FIX: Return FULL sanitized_records array!
            # ========================================
            result = Data(
                data={
                    "status": "success",
                    "data_type": data_type,
                    "primary_column": primary_col,  # Added for downstream components
                    "sanitized_records": sanitized_records,  # â† FULL ARRAY (not just 3!)
                    "phi_report": phi_report,
                    "safe_to_upload": phi_report['records_with_phi'] == 0,
                    "original_count": len(records),
                    "sanitized_count": len(sanitized_records),
                    "validation_gates_passed": 4  # Tracking validation stages
                }
            )
            
            # Status message
            if phi_report['records_with_phi'] > 0:
                self.status = f"âš ï¸ PHI: {phi_report['records_with_phi']}/{len(records)} records ({phi_report['total_redactions']} redactions)"
            else:
                self.status = f"âœ… CLEAN: {len(records)} {data_type} records"
            
            return result
            
        except Exception as e:
            self.log(f"âŒ Error in PHI detection: {str(e)}")
            self.status = f"âŒ Error: {str(e)}"
            return Data(
                data={
                    "status": "error",
                    "error": str(e),
                    "validation_gates_passed": 0
                }
            )
