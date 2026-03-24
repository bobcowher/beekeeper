"""
Agent SDK Generator

Generates a self-contained Python SDK file for AI agent integration with Beekeeper projects.
Each SDK is pre-configured with project-specific details (URL, name, etc.).
"""

from datetime import datetime
from textwrap import dedent


def generate_sdk(project_name: str, base_url: str, project) -> str:
    """
    Generate a complete Python SDK file for the given project.

    Args:
        project_name: Name of the Beekeeper project
        base_url: Base URL of the Beekeeper server (e.g., "http://192.168.1.57:5000")
        project: Project object with details

    Returns:
        Complete Python SDK file content as a string
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = project_name.replace('-', '_')

    sdk_content = f'''"""
Beekeeper Python SDK - Auto-generated for project: {project_name}

This file was generated on {timestamp} and contains everything needed
to control the Beekeeper training project via API.

Quick Start:
    from beekeeper_client_{safe_name} import BeekeeperClient

    client = BeekeeperClient(api_key="your_api_key_here")
    client.start_training()
    metrics = client.get_latest_metrics()

Requirements:
    pip install requests

Documentation: {base_url}/api/v1/projects/{project_name}
"""

import requests
from typing import Iterator, Optional, List, Dict, Any
from urllib.parse import urljoin


# ============================================================================
# PROJECT CONFIGURATION (AUTO-GENERATED)
# ============================================================================

PROJECT_NAME = "{project_name}"
BASE_URL = "{base_url}"
API_VERSION = "v1"

# Project Details
GIT_URL = "{project.git_url}"
BRANCH = "{project.branch}"
SETUP_STATUS = "{project.setup_status}"


# ============================================================================
# BEEKEEPER CLIENT
# ============================================================================

class BeekeeperClient:
    """Client for controlling Beekeeper training project: {project_name}"""

    def __init__(self, api_key: str, base_url: str = BASE_URL):
        """
        Initialize Beekeeper client.

        Args:
            api_key: Your Beekeeper API key (generate in web UI)
            base_url: API base URL (defaults to project server)
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.project_name = PROJECT_NAME
        self.session = requests.Session()
        self.session.headers.update({{
            'X-API-Key': self.api_key,
            'Content-Type': 'application/json'
        }})

    def _build_url(self, endpoint: str) -> str:
        """Build full API URL from endpoint."""
        endpoint = endpoint.lstrip('/')
        return f"{{self.base_url}}/api/{{API_VERSION}}/projects/{{self.project_name}}/{{endpoint}}"

    def _request(self, method: str, endpoint: str, **kwargs) -> Dict[str, Any]:
        """
        Make HTTP request to Beekeeper API.

        Args:
            method: HTTP method (GET, POST, DELETE)
            endpoint: API endpoint path
            **kwargs: Additional arguments passed to requests

        Returns:
            Parsed JSON response

        Raises:
            requests.HTTPError: If request fails
        """
        url = self._build_url(endpoint)
        response = self.session.request(method, url, **kwargs)
        return self._handle_response(response)

    def _handle_response(self, response: requests.Response) -> Dict[str, Any]:
        """
        Handle API response with error checking.

        Args:
            response: HTTP response object

        Returns:
            Parsed JSON response

        Raises:
            requests.HTTPError: If response indicates error
        """
        try:
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as e:
            error_data = {{}}
            try:
                error_data = response.json()
            except:
                pass

            error_msg = error_data.get('error', {{}}).get('message', str(e))
            raise requests.HTTPError(
                f"API Error: {{error_msg}}",
                response=response
            )

    # ========================================================================
    # TRAINING CONTROL
    # ========================================================================

    def start_training(self) -> Dict[str, Any]:
        """
        Start training run.

        Returns:
            dict: Response with status and message

        Example:
            >>> result = client.start_training()
            >>> print(result['message'])
        """
        return self._request('POST', 'training/start')

    def stop_training(self) -> Dict[str, Any]:
        """
        Stop training run.

        Returns:
            dict: Response with status and message

        Example:
            >>> result = client.stop_training()
            >>> print(result['message'])
        """
        return self._request('POST', 'training/stop')

    def get_training_status(self) -> Dict[str, Any]:
        """
        Get current training status.

        Returns:
            dict: Training status information

        Example:
            >>> status = client.get_training_status()
            >>> print(status['data']['status'])
        """
        return self._request('GET', 'training/status')

    # ========================================================================
    # LOGS
    # ========================================================================

    def get_logs(self, tail: Optional[int] = None) -> Dict[str, Any]:
        """
        Get log content.

        Args:
            tail: Optional number of lines from end of log

        Returns:
            dict: Log content and metadata

        Example:
            >>> logs = client.get_logs(tail=100)
            >>> print(logs['data']['content'])
        """
        params = {{'tail': tail}} if tail else {{}}
        return self._request('GET', 'logs', params=params)

    def stream_logs(self) -> Iterator[str]:
        """
        Stream logs in real-time using Server-Sent Events.

        Yields:
            str: Log lines as they are written

        Example:
            >>> for line in client.stream_logs():
            ...     print(line, end='')
        """
        url = self._build_url('logs/stream')
        response = self.session.get(url, stream=True)
        response.raise_for_status()

        for line in response.iter_lines(decode_unicode=True):
            if line.startswith('data: '):
                yield line[6:] + '\\n'

    # ========================================================================
    # FILES
    # ========================================================================

    def list_files(self, path: str = "") -> Dict[str, Any]:
        """
        List workspace files.

        Args:
            path: Optional subdirectory path

        Returns:
            dict: File listing with metadata

        Example:
            >>> files = client.list_files()
            >>> for f in files['data']['files']:
            ...     print(f['name'])
        """
        params = {{'path': path}} if path else {{}}
        return self._request('GET', 'files', params=params)

    def download_file(self, filepath: str, destination: Optional[str] = None) -> str:
        """
        Download a specific file from workspace.

        Args:
            filepath: Path to file in workspace
            destination: Local path to save file (defaults to same filename)

        Returns:
            str: Path where file was saved

        Example:
            >>> path = client.download_file('train.py', './train.py')
            >>> print(f"Downloaded to {{path}}")
        """
        import os

        url = self._build_url(f'files/{{filepath}}')
        response = self.session.get(url)
        response.raise_for_status()

        if destination is None:
            destination = os.path.basename(filepath)

        with open(destination, 'wb') as f:
            f.write(response.content)

        return destination

    def download_workspace(self, destination: str = "workspace.zip") -> str:
        """
        Download entire workspace as zip file.

        Args:
            destination: Local path to save zip file

        Returns:
            str: Path where zip was saved

        Example:
            >>> path = client.download_workspace('./my_workspace.zip')
            >>> print(f"Workspace downloaded to {{path}}")
        """
        url = self._build_url('files/download')
        response = self.session.get(url)
        response.raise_for_status()

        with open(destination, 'wb') as f:
            f.write(response.content)

        return destination

    # ========================================================================
    # TENSORBOARD METRICS
    # ========================================================================

    def get_latest_metrics(
        self,
        detail: str = 'low',
        metrics: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get metrics from the latest training run.

        Args:
            detail: Analysis detail level ('low', 'medium', 'high')
            metrics: Optional list of specific metric names to fetch

        Returns:
            dict: Metrics data with trend analysis

        Example:
            >>> metrics = client.get_latest_metrics(detail='medium')
            >>> for name, data in metrics['data']['metrics'].items():
            ...     print(f"{{name}}: {{data['trend']}}")
        """
        params = {{'detail': detail}}
        if metrics:
            params['metrics'] = ','.join(metrics)
        return self._request('GET', 'metrics/latest', params=params)

    def get_run_metrics(
        self,
        run_id: int,
        detail: str = 'low',
        metrics: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Get metrics from a specific training run.

        Args:
            run_id: Training run ID
            detail: Analysis detail level ('low', 'medium', 'high')
            metrics: Optional list of specific metric names to fetch

        Returns:
            dict: Metrics data with trend analysis

        Example:
            >>> metrics = client.get_run_metrics(5, detail='high')
            >>> print(metrics['data']['metrics'])
        """
        params = {{'detail': detail}}
        if metrics:
            params['metrics'] = ','.join(metrics)
        return self._request('GET', f'metrics/run/{{run_id}}', params=params)

    # ========================================================================
    # RUN HISTORY
    # ========================================================================

    def get_run_history(self, limit: int = 20) -> Dict[str, Any]:
        """
        Get training run history.

        Args:
            limit: Maximum number of runs to return

        Returns:
            dict: List of training runs with metadata

        Example:
            >>> history = client.get_run_history(limit=10)
            >>> for run in history['data']['runs']:
            ...     print(f"Run #{{run['id']}}: {{run['status']}}")
        """
        params = {{'limit': limit}}
        return self._request('GET', 'runs', params=params)

    def get_run_details(self, run_id: int) -> Dict[str, Any]:
        """
        Get details for a specific training run.

        Args:
            run_id: Training run ID

        Returns:
            dict: Run details and metadata

        Example:
            >>> details = client.get_run_details(5)
            >>> print(details['data']['run'])
        """
        return self._request('GET', f'runs/{{run_id}}')

    def download_run_log(self, run_id: int, destination: Optional[str] = None) -> str:
        """
        Download log file for a specific training run.

        Args:
            run_id: Training run ID
            destination: Local path to save log (defaults to run_<id>.log)

        Returns:
            str: Path where log was saved

        Example:
            >>> path = client.download_run_log(5, './run5.log')
            >>> print(f"Log saved to {{path}}")
        """
        url = self._build_url(f'runs/{{run_id}}/log')
        response = self.session.get(url)
        response.raise_for_status()

        if destination is None:
            destination = f'run_{{run_id}}.log'

        with open(destination, 'wb') as f:
            f.write(response.content)

        return destination

    def clear_run_history(self) -> Dict[str, Any]:
        """
        Clear all training run history.

        Returns:
            dict: Response with status and message

        Example:
            >>> result = client.clear_run_history()
            >>> print(result['message'])
        """
        return self._request('DELETE', 'runs')

    # ========================================================================
    # PROJECT INFO
    # ========================================================================

    def get_project_info(self) -> Dict[str, Any]:
        """
        Get project details and configuration.

        Returns:
            dict: Project information

        Example:
            >>> info = client.get_project_info()
            >>> print(info['data']['project'])
        """
        url = f"{{self.base_url}}/api/{{API_VERSION}}/projects/{{self.project_name}}"
        response = self.session.get(url)
        return self._handle_response(response)

    def get_system_stats(self) -> Dict[str, Any]:
        """
        Get system statistics (CPU, RAM, GPU).

        Returns:
            dict: System resource usage

        Example:
            >>> stats = client.get_system_stats()
            >>> print(f"GPU Usage: {{stats['data']['gpu']['utilization']}}%")
        """
        url = f"{{self.base_url}}/api/{{API_VERSION}}/stats"
        response = self.session.get(url)
        return self._handle_response(response)


# ============================================================================
# EXAMPLE USAGE
# ============================================================================

if __name__ == "__main__":
    import sys

    # Check for API key
    if len(sys.argv) < 2:
        print("Usage: python beekeeper_client_{safe_name}.py <api_key>")
        print("\\nOr set API_KEY environment variable and run examples")
        sys.exit(1)

    api_key = sys.argv[1]

    # Initialize client
    client = BeekeeperClient(api_key=api_key)
    print(f"Connected to Beekeeper project: {{PROJECT_NAME}}")
    print(f"Server: {{BASE_URL}}\\n")

    # Example 1: Get project info
    print("=" * 60)
    print("EXAMPLE 1: Get Project Info")
    print("=" * 60)
    info = client.get_project_info()
    project = info['data']['project']
    print(f"Name: {{project['name']}}")
    print(f"Git URL: {{project['git_url']}}")
    print(f"Branch: {{project['branch']}}")
    print(f"Setup Status: {{project['setup_status']}}")
    print(f"Training Status: {{project['train_status']}}")
    print()

    # Example 2: Get system stats
    print("=" * 60)
    print("EXAMPLE 2: System Stats")
    print("=" * 60)
    stats = client.get_system_stats()
    data = stats['data']
    print(f"CPU Usage: {{data['cpu']['percent']}}%")
    print(f"RAM Usage: {{data['memory']['percent']}}%")
    if data['gpu']['available']:
        print(f"GPU Usage: {{data['gpu']['utilization']}}%")
        print(f"GPU Memory: {{data['gpu']['memory_used']}}MB / {{data['gpu']['memory_total']}}MB")
    print()

    # Example 3: Start training (commented out - uncomment to use)
    # print("=" * 60)
    # print("EXAMPLE 3: Start Training")
    # print("=" * 60)
    # result = client.start_training()
    # print(f"Status: {{result['status']}}")
    # print(f"Message: {{result['message']}}")
    # print()

    # Example 4: Get logs
    print("=" * 60)
    print("EXAMPLE 4: Get Recent Logs (last 20 lines)")
    print("=" * 60)
    logs = client.get_logs(tail=20)
    if logs['data']['content']:
        print(logs['data']['content'])
    else:
        print("(No logs available)")
    print()

    # Example 5: Get run history
    print("=" * 60)
    print("EXAMPLE 5: Training Run History")
    print("=" * 60)
    history = client.get_run_history(limit=5)
    runs = history['data']['runs']
    if runs:
        for run in runs:
            print(f"Run #{{run['id']}}: {{run['status']}} - Started: {{run['started_at']}}")
            if run['ended_at']:
                print(f"  Ended: {{run['ended_at']}} (Duration: {{run['duration']}})")
    else:
        print("(No training runs yet)")
    print()

    # Example 6: Get latest metrics (if available)
    print("=" * 60)
    print("EXAMPLE 6: Latest Metrics Analysis")
    print("=" * 60)
    try:
        metrics = client.get_latest_metrics(detail='medium')
        if metrics['data']['metrics']:
            for metric_name, data in metrics['data']['metrics'].items():
                print(f"\\n{{metric_name}}:")
                print(f"  Trend: {{data['trend']}}")
                print(f"  Final Value: {{data['final_value']:.4f}}")
                print(f"  Improvement: {{data['improvement_percent']:.1f}}%")
                print(f"  Summary: {{data['summary']}}")
        else:
            print("(No metrics available)")
    except Exception as e:
        print(f"(Metrics not available: {{e}})")
    print()

    # Example 7: List workspace files
    print("=" * 60)
    print("EXAMPLE 7: Workspace Files")
    print("=" * 60)
    files = client.list_files()
    print("Files in workspace:")
    for file in files['data']['files']:
        print(f"  {{file['name']}} ({{file['size']}} bytes)")
    print()

    # Example 8: Stream logs in real-time (commented out - blocks until Ctrl+C)
    # print("=" * 60)
    # print("EXAMPLE 8: Stream Logs (Ctrl+C to stop)")
    # print("=" * 60)
    # try:
    #     for line in client.stream_logs():
    #         print(line, end='')
    # except KeyboardInterrupt:
    #     print("\\nStopped streaming")

    print("\\nAll examples completed!")
    print("Edit this file to uncomment and run additional examples.")
'''

    return sdk_content
