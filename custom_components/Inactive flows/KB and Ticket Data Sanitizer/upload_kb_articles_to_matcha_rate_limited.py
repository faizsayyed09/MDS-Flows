from langflow.custom import Component
from langflow.io import Output, DataInput, MessageTextInput, SecretStrInput, IntInput
from langflow.schema import Data
import requests
import json
from datetime import datetime
import io
import time

class UploadToMatchaFileAPI(Component):
    display_name = "Upload KB Articles to Matcha (Rate Limited)"
    description = "Uploads PHI-sanitized KB articles with retry logic and rate limiting"
    
    inputs = [
        DataInput(
            name="processed_data",
            display_name="Processed Data",
            info="Output from Data Quality Processor"
        ),
        MessageTextInput(
            name="folder_id",
            display_name="Folder ID",
            info="Matcha folder ID for Knowledge Articles",
            value="29490"  
        ),
        SecretStrInput(
            name="api_key",
            display_name="Matcha API Key",
            info="API KEY",
            required=True
        ),
        IntInput(
            name="delay_seconds",
            display_name="Delay Between Uploads (seconds)",
            info="Time to wait between each upload to avoid rate limiting",
            value=2,  # Increased from 1 to 2 seconds
            advanced=True
        ),
        IntInput(
            name="max_retries",
            display_name="Max Retries",
            info="Number of retry attempts for failed uploads",
            value=5,  # Increased from 3 to 5
            advanced=True
        ),
        MessageTextInput(
            name="base_url",
            display_name="Matcha Base URL",
            value="https://matcha.harriscomputer.com",
            info="Matcha platform URL",
            advanced=True
        )
    ]
    
    outputs = [
        Output(display_name="Upload Results", name="output", method="upload_articles")
    ]
    
    def upload_articles(self) -> Data:
        """Upload KB articles as individual text files to Matcha with rate limiting"""
        try:
            processed_data = self.processed_data
            if hasattr(processed_data, 'data'):
                data_dict = processed_data.data
            else:
                data_dict = processed_data
            
            processed_records = data_dict.get('processed_records', [])
            
            self.log(f"Uploading {len(processed_records)} articles to folder {self.folder_id}")
            self.log(f"Rate limit: {self.delay_seconds}s delay between uploads")
            
            if not processed_records:
                return Data(data={
                    "status": "error",
                    "error": "No records to upload"
                })
            
            upload_stats = {
                'total_articles': len(processed_records),
                'uploaded': 0,
                'failed': 0,
                'retried': 0,
                'phi_sanitized_count': sum(1 for r in processed_records if r.get('__PHI_DETECTED__')),
                'errors': []
            }
            
            # Upload each article with rate limiting
            for idx, article in enumerate(processed_records):
                try:
                    kb_number = article.get('Number', f'KB_{idx}')
                    title = article.get('Short description', f'Article {idx}')
                    content = article.get('Article body', '')
                    
                    # Strip HTML
                    clean_content = self._strip_html(content)
                    
                    # Create markdown file content
                    file_content = f"""# {title}

**KB Number:** {kb_number}
**Category:** {article.get('Category', 'N/A')}
**Author:** {article.get('Author', 'N/A')}
**Knowledge Base:** {article.get('Knowledge base', 'N/A')}
"""
                    
                    if article.get('__PHI_DETECTED__'):
                        phi_types = article.get('__PHI_TYPES__', [])
                        file_content += f"âš ï¸ **PHI Sanitized:** Yes ({', '.join(phi_types)})\n\n"
                    
                    file_content += f"---\n\n{clean_content}\n\n---\n\n"
                    file_content += f"*Updated: {article.get('Updated', 'N/A')}*\n"
                    
                    # Upload with retry logic
                    success = False
                    last_error = None
                    
                    for retry in range(self.max_retries):
                        success, error_msg = self._upload_file_to_matcha(
                            filename=f"{kb_number}.md",
                            file_content=file_content
                        )
                        
                        if success:
                            upload_stats['uploaded'] += 1
                            if retry > 0:
                                upload_stats['retried'] += 1
                                self.log(f"âœ… {kb_number} uploaded (retry {retry})")
                            break
                        else:
                            last_error = error_msg
                            # If server error (500), wait longer before retry
                            if "500" in error_msg:
                                wait_time = self.delay_seconds * (2 ** retry)  # Exponential backoff
                                self.log(f"âš ï¸ Server error for {kb_number}, retrying in {wait_time}s...")
                                time.sleep(wait_time)
                            else:
                                # Other errors, don't retry
                                break
                    
                    # If all retries failed
                    if not success:
                        upload_stats['failed'] += 1
                        upload_stats['errors'].append({
                            'kb_number': kb_number,
                            'title': title,
                            'error': last_error
                        })
                    
                    # Rate limiting delay
                    if idx < len(processed_records) - 1:  # Don't delay after last article
                        time.sleep(self.delay_seconds)
                    
                    # Log progress
                    if (idx + 1) % 50 == 0:
                        self.log(f"Progress: {idx + 1}/{len(processed_records)} processed, {upload_stats['uploaded']} uploaded, {upload_stats['failed']} failed")
                
                except Exception as e:
                    upload_stats['failed'] += 1
                    upload_stats['errors'].append({
                        'kb_number': article.get('Number'),
                        'error': str(e)
                    })
            
            success_rate = (upload_stats['uploaded'] / upload_stats['total_articles'] * 100) if upload_stats['total_articles'] > 0 else 0
            
            result = Data(data={
                "status": "success" if upload_stats['failed'] == 0 else "partial_success",
                "upload_stats": upload_stats,
                "success_rate": f"{success_rate:.1f}%",
                "folder_id": self.folder_id,
                "folder_url": f"https://matcha.harriscomputer.com/matcha/objects/files?folder_id={self.folder_id}",
                "rate_limit_delay": f"{self.delay_seconds}s",
                "next_steps": [
                    f"1. Go to: https://matcha.harriscomputer.com/matcha/objects/files?folder_id={self.folder_id}",
                    f"2. Verify {upload_stats['uploaded']} files are visible",
                    "3. Click 'Analyze' to index uploaded files",
                    "4. If failures remain, increase delay_seconds parameter",
                    f"5. {upload_stats['phi_sanitized_count']} articles contain PHI-sanitized content"
                ]
            })
            
            self.status = f"âœ… Uploaded {upload_stats['uploaded']}/{upload_stats['total_articles']} articles ({upload_stats['failed']} failed, {upload_stats['retried']} retried)"
            
            return result
        
        except Exception as e:
            self.log(f"Error: {str(e)}")
            self.status = f"âŒ Error: {str(e)}"
            return Data(data={"status": "error", "error": str(e)})
    
    def _upload_file_to_matcha(self, filename: str, file_content: str) -> tuple:
        """
        Upload a single text file to Matcha using the official file upload API
        Returns: (success: bool, error_message: str)
        """
        try:
            # Official Matcha file upload endpoint
            upload_url = f"{self.base_url}/rest/api/v1/file"
            
            # Headers
            headers = {
                'MATCHA-API-KEY': self.api_key
            }
            
            # Query parameters
            params = {
                'folder_id': self.folder_id
            }
            
            # Create file-like object
            file_bytes = io.BytesIO(file_content.encode('utf-8'))
            
            # Multipart form data
            files = {
                'file': (filename, file_bytes, 'text/markdown')
            }
            
            # Make request
            response = requests.post(
                upload_url,
                headers=headers,
                params=params,
                files=files,
                timeout=30
            )
            
            # Check response
            if response.status_code in [200, 201]:
                return True, None
            else:
                error_msg = f"API returned {response.status_code}: {response.text[:200]}"
                return False, error_msg
        
        except Exception as e:
            return False, f"Exception: {str(e)}"
    
    def _strip_html(self, html_content: str) -> str:
        """Strip HTML tags from content"""
        if not html_content:
            return ""
        
        import re
        
        # Remove HTML tags
        clean_text = re.sub('<[^<]+?>', '', html_content)
        
        # Clean whitespace
        clean_text = re.sub(r'\s+', ' ', clean_text).strip()
        
        # Decode HTML entities
        clean_text = clean_text.replace('&nbsp;', ' ')
        clean_text = clean_text.replace('&amp;', '&')
        clean_text = clean_text.replace('&lt;', '<')
        clean_text = clean_text.replace('&gt;', '>')
        clean_text = clean_text.replace('&quot;', '"')
        
        return clean_text
