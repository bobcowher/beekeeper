"""TensorBoard metrics parsing and analysis service."""

import os
import json
import logging
from typing import Optional
from pathlib import Path

import numpy as np
from tbparse import SummaryReader

from services.db_service import get_db

log = logging.getLogger(__name__)


def parse_run_metrics(projects_dir: str, project_name: str, run_id: int) -> dict:
    """
    Parse TFEvents and cache analysis for all metrics.

    Args:
        projects_dir: Base projects directory
        project_name: Name of the project
        run_id: Training run ID

    Returns:
        dict with 'success': bool and optional 'reason' for failure
    """
    try:
        db = get_db()

        # Get run info from DB
        run = db.get_training_run(run_id)
        if not run or not run.get('tensorboard_dir'):
            log.warning(f"Run {run_id} has no tensorboard_dir")
            return {'success': False, 'reason': 'no_tensorboard_dir'}

        # Try the stored path first
        tb_dir = os.path.join(projects_dir, project_name, run['tensorboard_dir'])

        # If stored path doesn't exist, auto-discover TensorBoard data
        if not os.path.isdir(tb_dir):
            log.info(f"Stored TB path not found, attempting auto-discovery")
            try:
                discovered = _discover_tensorboard_dir(projects_dir, project_name, run)
                if discovered:
                    tb_dir = discovered
                    log.info(f"Auto-discovered TensorBoard data at: {tb_dir}")
                else:
                    log.warning(f"Auto-discovery found no matching TensorBoard directories")
                    return {'success': False, 'reason': 'directory_not_found', 'path': tb_dir}
            except Exception as e:
                log.error(f"Auto-discovery failed with exception: {e}", exc_info=True)
                return {'success': False, 'reason': 'directory_not_found', 'path': tb_dir}

        # Check if any event files exist
        event_files = list(Path(tb_dir).glob('**/events.out.tfevents.*'))
        if not event_files:
            log.warning(f"No TensorBoard event files in {tb_dir}")
            return {'success': False, 'reason': 'no_event_files', 'path': tb_dir}

        # Parse all metrics using tbparse
        try:
            reader = SummaryReader(tb_dir)
            scalars_df = reader.scalars

            if scalars_df.empty:
                log.warning(f"No scalar metrics found in {tb_dir} (files exist but empty/not flushed)")
                return {'success': False, 'reason': 'no_scalars', 'path': tb_dir, 'event_files': len(event_files)}

        except Exception as e:
            log.error(f"Failed to parse TensorBoard events in {tb_dir}: {e}")
            return {'success': False, 'reason': 'parse_error', 'error': str(e)}

        # Group by metric tag
        metric_groups = scalars_df.groupby('tag')

        log.info(f"Found {len(metric_groups)} metrics for run {run_id}")

        # Analyze each metric
        for metric_name, group in metric_groups:
            # Extract data as list of (step, value, wall_time)
            # Use wall_time if available, otherwise fall back to step
            if 'wall_time' in group.columns:
                data = list(zip(
                    group['step'].tolist(),
                    group['value'].tolist(),
                    group['wall_time'].tolist()
                ))
            else:
                # Fallback: use step as wall_time
                data = list(zip(
                    group['step'].tolist(),
                    group['value'].tolist(),
                    group['step'].tolist()
                ))

            # Sort by step
            data.sort(key=lambda x: x[0])

            # Analyze the metric
            analysis = analyze_metric(metric_name, data)

            # Save to database
            db.save_metric_analysis(run_id, metric_name, analysis)

        log.info(f"Successfully parsed and analyzed {len(metric_groups)} metrics for run {run_id}")
        return {'success': True}

    except Exception as e:
        log.error(f"Error parsing metrics for run {run_id}: {e}", exc_info=True)
        return {'success': False, 'reason': 'unexpected_error', 'error': str(e)}


def analyze_metric(metric_name: str, data: list) -> dict:
    """
    Analyze time series data for a metric.

    Args:
        metric_name: Name of the metric
        data: List of (step, value, wall_time) tuples

    Returns:
        Dictionary with analysis results
    """
    if len(data) < 2:
        return {
            'trend': 'insufficient_data',
            'total_points': len(data),
            'summary': 'Insufficient data for analysis (need at least 2 points)',
            'sampled_points': [{'step': d[0], 'value': d[1], 'wall_time': d[2]} for d in data]
        }

    steps = [d[0] for d in data]
    values = [d[1] for d in data]

    # Basic statistics
    initial_value = values[0]
    final_value = values[-1]
    best_value = min(values) if _is_lower_better(metric_name) else max(values)
    best_step = steps[values.index(best_value)]

    # Improvement percentage
    if initial_value != 0:
        improvement_percent = ((final_value - initial_value) / abs(initial_value)) * 100
    else:
        improvement_percent = 0.0

    # Trend detection
    trend = detect_trend(values, metric_name)

    # Convergence detection
    convergence = detect_convergence(data)

    # Anomaly detection
    anomalies = detect_anomalies(data)

    # Generate summary
    summary = _generate_summary(
        metric_name, trend, initial_value, final_value,
        improvement_percent, convergence, anomalies
    )

    # Smart sampling
    sampled_points = smart_sample(data)

    return {
        'trend': trend,
        'initial_value': float(initial_value),
        'final_value': float(final_value),
        'best_value': float(best_value),
        'best_step': int(best_step),
        'improvement_percent': float(improvement_percent),
        'converged': convergence['converged'],
        'convergence_step': convergence.get('step'),
        'anomaly_count': len(anomalies),
        'anomalies': anomalies,
        'summary': summary,
        'sampled_points': sampled_points,
        'total_points': len(data)
    }


def detect_trend(values: list, metric_name: str) -> str:
    """
    Detect trend in values using linear regression.

    Returns: 'improving', 'stable', 'unstable', 'insufficient_data'
    """
    if len(values) < 10:
        return 'insufficient_data'

    # Linear regression
    x = np.arange(len(values))
    y = np.array(values)

    # Fit line: y = mx + b
    coeffs = np.polyfit(x, y, 1)
    slope = coeffs[0]

    # Calculate R²
    y_pred = np.polyval(coeffs, x)
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0

    # Check if trend is stable (R² > 0.5 indicates good linear fit)
    if r_squared < 0.3:
        return 'unstable'

    # Determine if improving based on metric type and slope
    is_lower_better = _is_lower_better(metric_name)

    # Normalize slope by value range for significance test
    value_range = np.max(values) - np.min(values)
    if value_range > 0:
        normalized_slope = abs(slope) / (value_range / len(values))
    else:
        normalized_slope = 0

    # If slope is insignificant, it's stable
    if normalized_slope < 0.1:
        return 'stable'

    # Check if slope direction matches desired improvement
    if (is_lower_better and slope < 0) or (not is_lower_better and slope > 0):
        return 'improving'
    else:
        # Getting worse is also a stable trend, just not improving
        return 'stable' if r_squared > 0.5 else 'unstable'


def detect_convergence(data: list) -> dict:
    """
    Detect if metric has converged.

    Returns: {'converged': bool, 'step': int or None}
    """
    if len(data) < 20:
        return {'converged': False, 'step': None}

    # Look at last 20% of points
    window_size = max(10, len(data) // 5)
    last_values = [d[1] for d in data[-window_size:]]

    # Calculate coefficient of variation (std/mean)
    mean_val = np.mean(last_values)
    std_val = np.std(last_values)

    if abs(mean_val) > 1e-6:
        cv = abs(std_val / mean_val)
    else:
        cv = std_val

    # Converged if CV < 0.02 (2% variation)
    converged = cv < 0.02

    if converged:
        # Find approximate convergence point (where CV first drops below threshold)
        for i in range(window_size, len(data)):
            window = [d[1] for d in data[i-window_size:i]]
            m = np.mean(window)
            s = np.std(window)
            if abs(m) > 1e-6:
                cv_i = abs(s / m)
            else:
                cv_i = s

            if cv_i < 0.02:
                return {'converged': True, 'step': int(data[i][0])}

    return {'converged': converged, 'step': None}


def detect_anomalies(data: list) -> list:
    """
    Detect anomalies using IQR method.

    Returns: List of dicts with step, value, reason (max 5)
    """
    if len(data) < 10:
        return []

    values = np.array([d[1] for d in data])

    # IQR method
    q1 = np.percentile(values, 25)
    q3 = np.percentile(values, 75)
    iqr = q3 - q1

    if iqr == 0:
        return []

    lower_bound = q1 - 1.5 * iqr
    upper_bound = q3 + 1.5 * iqr

    anomalies = []
    for step, value, _ in data:
        if value > upper_bound:
            anomalies.append({
                'step': int(step),
                'value': float(value),
                'reason': 'spike_high'
            })
        elif value < lower_bound:
            anomalies.append({
                'step': int(step),
                'value': float(value),
                'reason': 'spike_low'
            })

    # Return top 5 most extreme
    if anomalies:
        # Sort by distance from bounds
        anomalies.sort(
            key=lambda x: abs(x['value'] - q1) if x['value'] < lower_bound else abs(x['value'] - q3),
            reverse=True
        )
        return anomalies[:5]

    return []


def smart_sample(data: list, target: int = 100) -> list:
    """
    Smart sample ~100 key points from data.

    Returns: List of dicts with step, value, wall_time
    """
    if len(data) <= target:
        return [{'step': int(d[0]), 'value': float(d[1]), 'wall_time': float(d[2])} for d in data]

    sampled = []
    indices = set()

    values = [d[1] for d in data]

    # Always include: first, last, min, max
    indices.add(0)
    indices.add(len(data) - 1)
    indices.add(values.index(min(values)))
    indices.add(values.index(max(values)))

    # Add inflection points (derivative sign changes)
    if len(data) > 2:
        derivatives = np.diff([d[1] for d in data])
        sign_changes = np.diff(np.sign(derivatives))
        inflection_indices = np.where(sign_changes != 0)[0] + 1

        # Add inflection points (limit to avoid too many)
        for idx in inflection_indices[:20]:
            indices.add(int(idx))

    # Fill remaining with uniform samples
    remaining = target - len(indices)
    if remaining > 0:
        step_size = len(data) / (remaining + 1)
        for i in range(1, remaining + 1):
            idx = int(i * step_size)
            if 0 <= idx < len(data):
                indices.add(idx)

    # Convert to sorted list
    indices = sorted(list(indices))

    return [
        {
            'step': int(data[i][0]),
            'value': float(data[i][1]),
            'wall_time': float(data[i][2])
        }
        for i in indices
    ]


def get_metric_analysis(
    projects_dir: str,
    project_name: str,
    run_id: int,
    metric_filter: list = None,
    detail: str = 'low'
) -> dict:
    """
    Get metric analysis with specified detail level.

    Args:
        projects_dir: Base projects directory
        project_name: Name of the project
        run_id: Training run ID
        metric_filter: List of metric names to filter (None = all)
        detail: 'low' (summary), 'medium' (+samples), 'high' (+full data)

    Returns:
        Dictionary with analysis results or error info
    """
    db = get_db()

    # Check cache
    cached = db.get_metric_analyses(run_id, metric_filter)

    # If not cached, try to parse
    if not cached:
        result = parse_run_metrics(projects_dir, project_name, run_id)
        if not result['success']:
            # Provide diagnostic error messages
            reason = result.get('reason', 'unknown')
            if reason == 'no_tensorboard_dir':
                message = 'Run has no TensorBoard directory configured'
            elif reason == 'directory_not_found':
                message = f"TensorBoard directory not found: {result.get('path', 'unknown')}"
            elif reason == 'no_event_files':
                message = (f"TensorBoard directory exists but contains no event files. "
                          f"Check if the training script is writing TensorBoard data. "
                          f"Path: {result.get('path', 'unknown')}")
            elif reason == 'no_scalars':
                message = (f"TensorBoard event files exist ({result.get('event_files', 0)} files) "
                          f"but contain no scalar metrics. This usually means the data hasn't been "
                          f"flushed yet. Try calling writer.flush() in your training script, or wait "
                          f"longer for automatic flushing.")
            elif reason == 'parse_error':
                message = f"Failed to parse TensorBoard events: {result.get('error', 'unknown')}"
            else:
                message = f"Failed to parse TensorBoard data: {reason}"

            return {
                'error': 'NO_TENSORBOARD_DATA',
                'message': message,
                'diagnostic': result
            }

        # Fetch after parsing
        cached = db.get_metric_analyses(run_id, metric_filter)

    # If still empty after parse attempt
    if not cached:
        return {
            'error': 'METRIC_NOT_FOUND',
            'message': 'No metrics found matching the filter'
        }

    # Format response based on detail level
    result = {}

    for metric_name, analysis in cached.items():
        metric_data = {
            'trend': analysis['trend'],
            'initial_value': analysis['initial_value'],
            'final_value': analysis['final_value'],
            'best_value': analysis['best_value'],
            'best_step': analysis['best_step'],
            'improvement_percent': analysis['improvement_percent'],
            'converged': analysis['converged'],
            'convergence_step': analysis['convergence_step'],
            'anomaly_count': analysis['anomaly_count'],
            'anomalies': analysis['anomalies'],
            'summary': analysis['summary'],
            'total_points': analysis['total_points']
        }

        # Add sampled points for medium detail
        if detail in ['medium', 'high']:
            metric_data['sampled_points'] = analysis['sampled_points']

        # For high detail, include full raw data
        if detail == 'high':
            # Would need to re-parse from TFEvents - skip for now since it's expensive
            # This is intentionally left as sampled_points for performance
            pass

        result[metric_name] = metric_data

    return {'metrics': result}


def _discover_tensorboard_dir(projects_dir: str, project_name: str, run: dict) -> Optional[str]:
    """
    Auto-discover TensorBoard directory when the stored path doesn't exist.

    Searches common locations for TensorBoard event files created near the run's start time.

    Args:
        projects_dir: Base projects directory
        project_name: Name of the project
        run: Run dictionary with 'started_at' timestamp

    Returns:
        Absolute path to discovered TensorBoard directory, or None
    """
    import datetime
    from dateutil import parser as date_parser

    log.debug(f"Auto-discovering TB dir for project={project_name}, run_id={run.get('id')}")

    project_dir = os.path.join(projects_dir, project_name)

    # Parse run start time
    try:
        run_start = date_parser.parse(run['started_at'])
        log.debug(f"Looking for TB data for run started at {run_start}")
    except Exception as e:
        log.warning(f"Could not parse run start time: {run.get('started_at')}: {e}")
        return None

    # Common TensorBoard locations to search
    search_paths = [
        os.path.join(project_dir, 'workspace', 'runs'),
        os.path.join(project_dir, 'workspace', 'logs'),
        os.path.join(project_dir, 'workspace', 'tensorboard'),
        os.path.join(project_dir, 'runs'),
        os.path.join(project_dir, 'logs'),
        os.path.join(project_dir, 'tensorboard'),
        os.path.join(project_dir, 'workspace'),
    ]

    candidates = []

    # Search for directories with TensorBoard event files
    for search_path in search_paths:
        if not os.path.isdir(search_path):
            continue

        # Look for event files in this directory and subdirectories
        for root, dirs, files in os.walk(search_path):
            event_files = [f for f in files if f.startswith('events.out.tfevents.')]
            if event_files:
                # Found a directory with event files
                # Parse timestamps from TensorBoard event filenames
                # Format: events.out.tfevents.{timestamp}.{hostname}.{pid}.{suffix}
                import re
                timestamps = []
                for filename in event_files:
                    match = re.search(r'events\.out\.tfevents\.(\d+)', filename)
                    if match:
                        timestamps.append(int(match.group(1)))

                if not timestamps:
                    # Fallback to mtime if filename parsing fails
                    event_paths = [os.path.join(root, f) for f in event_files]
                    timestamps = [int(os.path.getmtime(p)) for p in event_paths]

                oldest_timestamp = min(timestamps)
                newest_timestamp = max(timestamps)

                # Convert to datetime
                oldest_dt = datetime.datetime.fromtimestamp(oldest_timestamp)
                newest_dt = datetime.datetime.fromtimestamp(newest_timestamp)

                # Check if this matches our run timeframe
                # Allow some tolerance (events might start slightly after run start)
                time_diff_start = abs((oldest_dt - run_start).total_seconds())
                time_diff_end = abs((newest_dt - run_start).total_seconds())

                # Consider it a match if event file timestamps overlap with run time
                # (within 1 hour of run start, or if run was active when files were written)
                if time_diff_start < 3600 or time_diff_end < 3600:
                    candidates.append({
                        'path': root,
                        'time_diff': min(time_diff_start, time_diff_end),
                        'event_count': len(event_files),
                        'oldest': oldest_dt,
                        'newest': newest_dt
                    })

    if not candidates:
        return None

    # Sort by time proximity and number of event files
    # Prefer directories with events closest to run start time
    candidates.sort(key=lambda c: (c['time_diff'], -c['event_count']))

    return candidates[0]['path']


def _is_lower_better(metric_name: str) -> bool:
    """Determine if lower values are better for this metric."""
    lower_better_keywords = ['loss', 'error', 'mse', 'mae', 'rmse', 'perplexity']
    metric_lower = metric_name.lower()
    return any(keyword in metric_lower for keyword in lower_better_keywords)


def _generate_summary(
    metric_name: str,
    trend: str,
    initial_value: float,
    final_value: float,
    improvement_percent: float,
    convergence: dict,
    anomalies: list
) -> str:
    """Generate human-readable summary of metric analysis."""
    parts = []

    # Trend and improvement
    if trend == 'improving':
        direction = 'improved' if _is_lower_better(metric_name) else 'increased'
        parts.append(
            f"{metric_name} {direction} by {abs(improvement_percent):.1f}% "
            f"from {initial_value:.3g} to {final_value:.3g}"
        )
    elif trend == 'stable':
        parts.append(
            f"{metric_name} remained relatively stable "
            f"(from {initial_value:.3g} to {final_value:.3g})"
        )
    elif trend == 'unstable':
        parts.append(
            f"{metric_name} showed unstable behavior "
            f"(from {initial_value:.3g} to {final_value:.3g})"
        )
    else:
        parts.append(f"{metric_name}: insufficient data for trend analysis")

    # Convergence
    if convergence['converged']:
        parts.append(f"Converged at step {convergence['step']}")

    # Anomalies
    if anomalies:
        parts.append(f"{len(anomalies)} anomalies detected")

    return '. '.join(parts) + '.'
