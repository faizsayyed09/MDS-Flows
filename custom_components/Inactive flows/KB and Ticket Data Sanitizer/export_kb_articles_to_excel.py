from langflow.custom import Component
from langflow.io import Output, DataInput, MessageTextInput
from langflow.schema import Data
import pandas as pd
from datetime import datetime
from pathlib import Path

class ExportKBToExcel(Component):
    display_name = "Export KB Articles to Excel"
    description = "Exports PHI-sanitized KB articles to Excel for manual upload"
    
    inputs = [
        DataInput(
            name="processed_data",
            display_name="Processed Data",
            info="Output from Data Quality Processor"
        ),
        MessageTextInput(
            name="export_path",
            display_name="Export Directory",
            value="/tmp",
            info="Directory to save Excel file",
            advanced=True
        )
    ]
    
    outputs = [
        Output(display_name="Export Result", name="output", method="export_to_excel")
    ]
    
    def export_to_excel(self) -> Data:
        """Export processed KB articles to Excel file"""
        try:
            processed_data = self.processed_data
            if hasattr(processed_data, 'data'):
                data_dict = processed_data.data
            else:
                data_dict = processed_data
            
            processed_records = data_dict.get('processed_records', [])
            data_type = data_dict.get('data_type', 'KB_ARTICLES')
            
            self.log(f"Exporting {len(processed_records)} articles to Excel")
            
            # Prepare data for Excel
            export_data = []
            for article in processed_records:
                export_data.append({
                    'KB_Number': article.get('Number'),
                    'Title': article.get('Short description'),
                    'Category': article.get('Category'),
                    'Author': article.get('Author'),
                    'Knowledge_Base': article.get('Knowledge base'),
                    'State': article.get('State'),
                    'Updated': str(article.get('Updated', '')),
                    'Content_Clean': self._strip_html(article.get('Article body', '')),
                    'Content_Original_HTML': article.get('Article body', ''),
                    'PHI_Detected': article.get('__PHI_DETECTED__', False),
                    'PHI_Types': ', '.join(article.get('__PHI_TYPES__', [])),
                    'Attachment_Link': article.get('Attachment link', False),
                    'Description': article.get('Description', '')
                })
            
            # Create DataFrame
            df = pd.DataFrame(export_data)
            
            # Generate filename with timestamp
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"KB_Articles_PHI_Sanitized_{timestamp}.xlsx"
            export_dir = Path(self.export_path)
            export_dir.mkdir(parents=True, exist_ok=True)
            filepath = export_dir / filename
            
            # Export to Excel with formatting
            with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='KB Articles', index=False)
                
                # Auto-adjust column widths
                worksheet = writer.sheets['KB Articles']
                for idx, col in enumerate(df.columns):
                    max_length = max(
                        df[col].astype(str).map(len).max(),
                        len(col)
                    ) + 2
                    worksheet.column_dimensions[chr(65 + idx)].width = min(max_length, 50)
                
                # Calculate statistics
                phi_count = df['PHI_Detected'].sum()
                phi_rate = (phi_count / len(df) * 100) if len(df) > 0 else 0
                
                # Create summary sheet
                summary_data = {
                    'Metric': [
                        'Total Articles',
                        'PHI Sanitized',
                        'PHI Sanitization Rate',
                        'Data Type',
                        'Export Date',
                        'Original Count',
                        'Quality Filtered',
                        'Ready for Upload'
                    ],
                    'Value': [
                        len(df),
                        phi_count,
                        f"{phi_rate:.1f}%",
                        data_type,
                        datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        data_dict.get('original_count', len(df)),
                        data_dict.get('original_count', len(df)) - len(df),
                        'Yes - Manual upload to Matcha folder 29485'
                    ]
                }
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Summary', index=False)
            
            self.log(f"âœ… Excel file created: {filepath}")
            
            result = Data(data={
                "status": "success",
                "file_path": str(filepath),
                "filename": filename,
                "total_records": len(df),
                "phi_sanitized_count": int(phi_count),
                "phi_sanitization_rate": f"{phi_rate:.1f}%",
                "export_timestamp": datetime.now().isoformat(),
                "next_steps": [
                    "1. Download the Excel file from the path shown above",
                    "2. Go to: https://matcha.harriscomputer.com/matcha/objects/files?folder_id=29485",
                    "3. Click 'Upload' or '+' button",
                    "4. Upload the Excel file",
                    "5. Click 'Analyze' to index articles",
                    "6. Test chatbot with sample questions"
                ]
            })
            
            self.status = f"âœ… Exported {len(df)} articles to {filename}"
            return result
            
        except Exception as e:
            self.log(f"âŒ Export error: {str(e)}")
            self.status = f"âŒ Error: {str(e)}"
            return Data(data={
                "status": "error",
                "error": str(e)
            })
    
    def _strip_html(self, html_content: str) -> str:
        """Strip HTML tags from content"""
        if not html_content:
            return ""
        
        import re
        clean_text = re.sub('<[^<]+?>', '', html_content)
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        clean_text = clean_text.replace('&nbsp;', ' ')
        clean_text = clean_text.replace('&amp;', '&')
        clean_text = clean_text.replace('&lt;', '<')
        clean_text = clean_text.replace('&gt;', '>')
        clean_text = clean_text.replace('&quot;', '"')
        return clean_text
