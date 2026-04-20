import datetime
from models.project import Project

def generate_sdk(project_name: str, base_url: str, project: Project) -> str:
    """
    Generate a standalone Python SDK for the specified project.
    """
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    git_url = project.git_url
    branch = project.branch
    setup_status = project.setup_status

    # Using a standard string and .replace() to avoid f-string nested quote issues
    sdk_template = """
\"\"\"
Beekeeper Python SDK - Auto-generated for project: {{PROJECT_NAME}}

This file was generated on {{TIMESTAMP}} and contains everything needed
to control the Beekeeper training project via API.

Quick Start:
    from beekeeper_client_{{PROJECT_NAME_SAFE}} import BeekeeperClient

    client = BeekeeperClient(api_key="your_api_key_here")
    client.start_training()
    metrics = client.get_latest_metrics()

Requirements:
    pip install requests

Documentation: {{BASE_URL}}/api/v1/projects/{{PROJECT_NAME}}
\"\"\"

import requests
import time
import json
from typing import Optional, Dict, List, Iterator, Any

# Project Configuration (auto-generated)
PROJECT_NAME = "{{PROJECT_NAME}}"
BASE_URL = "{{BASE_URL}}"
API_VERSION = "v1"

# Project Details
GIT_URL = "{{GIT_URL}}"
BRANCH = "{{BRANCH}}"
SETUP_STATUS = "{{SETUP_STATUS}}"

class BeekeeperClient:
    \"\"\"Client for controlling Beekeeper training project: {{PROJECT_NAME}}\"\"\"

    def __init__(self, api_key: str, base_url: str = BASE_URL):
        \"\"\"
        Initialize Beekeeper client.

        Args:
            api_key: Your Beekeeper API key (generate in web UI)
            base_url: API base URL (defaults to project server)
        \"\"\"
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.project_name = PROJECT_NAME
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {self.api_key}',
            'Content-Type': 'application/json'
        })

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        \"\"\"Internal request handler with error handling.\"\"\"
        url = f"{self.base_url}/api/v1/{endpoint.lstrip('/')}"
        try:
            response = self.session.request(method, url, **kwargs)
            return self._handle_response(response)
        except requests.exceptions.RequestException as e:
            return {"success": False, "error": {"code": "CONNECTION_ERROR", "message": str(e)}}

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        \"\"\"Parse response and handle common errors.\"\"\"
        try:
            data = response.json()
            if not response.ok and 'error' not in data:
                return {
                    "success": False, 
                    "error": {"code": f"HTTP_{response.status_code}", "message": response.text}
                }
            return data
        except ValueError:
            return {
                "success": False, 
                "error": {"code": "PARSE_ERROR", "message": f"Invalid JSON response: {response.text[:100]}"}
            }

    # Training Control
    def start_training(self) -> Dict[str, Any]:
        \"\"\"Start training run.\"\"\"
        return self._request('POST', f"/projects/{self.project_name}/training/start")

    def stop_training(self) -> Dict[str, Any]:
        \"\"\"Stop training run.\"\"\"
        return self._request('POST', f"/projects/{self.project_name}/training/stop")

    def get_training_status(self) -> Dict[str, Any]:
        \"\"\"Get current training status.\"\"\"
        return self._request('GET', f"/projects/{self.project_name}/training/status")

    # Logs
    def get_logs(self, tail: Optional[int] = None) -> Dict[str, Any]:
        \"\"\"Get log content.\"\"\"
        params = {'tail': tail} if tail else {}
        return self._request('GET', f"/projects/{self.project_name}/logs", params=params)

    def get_log_analysis(self) -> Dict[str, Any]:
        \"\"\"Get episode-based trend analysis from logs.\"\"\"
        return self._request('GET', f"/projects/{self.project_name}/logs/analysis")

    def stream_logs(self) -> Iterator[str]:
        \"\"\"Stream logs in real-time (SSE).\"\"\"
        url = f"{self.base_url}/api/v1/projects/{self.project_name}/logs/stream"
        response = self.session.get(url, stream=True)
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8')
                if decoded_line.startswith('data: '):
                    yield decoded_line[6:]

    # Files
    def list_files(self, path: str = "") -> Dict[str, Any]:
        \"\"\"List workspace files.\"\"\"
        return self._request('GET', f"/projects/{self.project_name}/files/{path}")

    def download_file(self, filepath: str, destination: Optional[str] = None) -> str:
        \"\"\"Download file to destination path.\"\"\"
        url = f"{self.base_url}/api/v1/projects/{self.project_name}/files/{filepath}"
        response = self.session.get(url, stream=True)
        response.raise_for_status()
        
        dest = destination or filepath.split('/')[-1]
        with open(dest, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return dest

    def download_workspace(self, destination: str = "workspace.zip") -> str:
        \"\"\"Download entire workspace as a zip file.\"\"\"
        url = f"{self.base_url}/api/v1/projects/{self.project_name}/files?zip=1"
        response = self.session.get(url, stream=True)
        response.raise_for_status()
        
        with open(destination, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        return destination

    # TensorBoard Metrics
    def get_latest_metrics(self, detail: str = 'low', metrics: Optional[List[str]] = None) -> Dict[str, Any]:
        \"\"\"
        Get latest run metrics with trend analysis.
        
        Args:
            detail: 'low' (summary), 'medium' (+samples)
            metrics: Optional list of metric names to fetch
        \"\"\"
        params = {'detail': detail}
        if metrics:
            params['metrics'] = ','.join(metrics)
        return self._request('GET', f"/projects/{self.project_name}/tensorboard/latest", params=params)

    def get_run_metrics(self, run_id: int, detail: str = 'low', metrics: Optional[List[str]] = None) -> Dict[str, Any]:
        \"\"\"Get metrics for a specific run ID.\"\"\"
        params = {'detail': detail}
        if metrics:
            params['metrics'] = ','.join(metrics)
        return self._request('GET', f"/runs/{run_id}/metrics", params=params)

    # Run History
    def get_run_history(self, limit: int = 20) -> Dict[str, Any]:
        \"\"\"Get training run history.\"\"\"
        return self._request('GET', f"/projects/{self.project_name}/runs", params={'limit': limit})

    def get_run_details(self, run_id: int) -> Dict[str, Any]:
        \"\"\"Get details for a specific run.\"\"\"
        return self._request('GET', f"/runs/{run_id}")

    # System Stats
    def get_system_stats(self) -> Dict[str, Any]:
        \"\"\"Get host system stats (CPU, RAM, GPU).\"\"\"
        return self._request('GET', "/stats")

# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import sys
    
    # Simple CLI for testing the generated SDK
    if len(sys.argv) < 2:
        print(f"Usage: python beekeeper_client_{PROJECT_NAME.replace('-', '_')}.py <API_KEY>")
        sys.exit(1)
        
    api_key = sys.argv[1]
    client = BeekeeperClient(api_key=api_key)
    
    print(f"--- Project: {PROJECT_NAME} ---")
    
    # 1. Check status
    status = client.get_training_status()
    print(f"Status: {status.get('data', {}).get('status', 'unknown')}")
    
    # 2. Get latest metrics
    print("\\nFetching latest metrics...")
    metrics_resp = client.get_latest_metrics(detail='low')
    if metrics_resp.get('success'):
        metrics = metrics_resp.get('data', {}).get('metrics', {})
        for name, data in metrics.items():
            print(f"- {data.get('summary', name)}")
    else:
        print(f"Error fetching metrics: {metrics_resp.get('error', {}).get('message')}")

    # 3. Get log analysis
    print("\\nFetching log-based analysis...")
    log_analysis = client.get_log_analysis()
    if log_analysis.get('success'):
        overall = log_analysis.get('data', {}).get('overall', {})
        print(f"Avg Reward: {overall.get('avg_reward')} (Trend: {log_analysis.get('data', {}).get('trend')})")
"""
    # Replace placeholders
    content = sdk_template.strip()
    content = content.replace("{{PROJECT_NAME}}", project_name)
    content = content.replace("{{PROJECT_NAME_SAFE}}", project_name.replace('-', '_'))
    content = content.replace("{{TIMESTAMP}}", timestamp)
    content = content.replace("{{BASE_URL}}", base_url)
    content = content.replace("{{GIT_URL}}", git_url)
    content = content.replace("{{BRANCH}}", branch)
    content = content.replace("{{SETUP_STATUS}}", setup_status)
    
    return content
