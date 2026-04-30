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


def ema_smooth(values: list, alpha: float = 0.9) -> list:
    """
    Exponential moving average smoothing, TensorBoard-style.

    alpha=0.9 means each point retains 90% of the previous smoothed value.
    Matches TensorBoard's smoothing slider behavior.
    """
    if not values:
        return []
    smoothed = [values[0]]
    for v in values[1:]:
        smoothed.append(alpha * smoothed[-1] + (1 - alpha) * v)
    return smoothed


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
            'sampled_points': [{'step': d[0], 'value': d[1], 'wall_time': d[2]} for d in data],
            'smoothed_points': [{'step': d[0], 'value': d[1], 'wall_time': d[2]} for d in data],
        }

    steps = [d[0] for d in data]
    values = [d[1] for d in data]

    # EMA-smoothed values (alpha=0.9 matches TensorBoard heavy smoothing)
    EMA_ALPHA = 0.9
    smoothed_values = ema_smooth(values, alpha=EMA_ALPHA)

    # Basic statistics (raw)
    initial_value = values[0]
    final_value = values[-1]
    lower_better = _is_lower_better(metric_name)
    best_value = min(values) if lower_better else max(values)
    best_step = steps[values.index(best_value)]

    # Improvement percentage (raw start → raw end)
    if initial_value != 0:
        improvement_percent = ((final_value - initial_value) / abs(initial_value)) * 100
    else:
        improvement_percent = 0.0

    # Trend detection (uses internal smoothing already)
    trend = detect_trend(values, metric_name)

    # Recent trend: last 20% of run, computed on EMA-smoothed values to avoid
    # linear regression being fooled by a late upturn preceded by noisy volatility
    recent_n = max(5, len(values) // 5)
    recent_trend = detect_trend(smoothed_values[-recent_n:], metric_name) if len(values) >= recent_n * 2 else trend

    # Peak detection on EMA-smoothed values — avoids one-episode outlier spikes
    # Scheduled metrics (epsilon, lr) intentionally decay — skip peak reversal.
    is_scheduled = _is_scheduled_metric(metric_name)
    smoothed_range = max(smoothed_values) - min(smoothed_values) if max(smoothed_values) != min(smoothed_values) else 1
    smoothed_final = smoothed_values[-1]

    # Late-window slope: slope of EMA over final 10% of steps, normalized by value range.
    # Positive = rising right now, negative = falling. More granular than recent_trend label.
    late_n = max(3, len(smoothed_values) // 10)
    late_window = smoothed_values[-late_n:]
    if len(late_window) >= 2 and smoothed_range > 0:
        late_coeffs = np.polyfit(np.arange(len(late_window)), late_window, 1)
        late_slope_pct = round(float(late_coeffs[0] * len(late_window) / smoothed_range * 100), 1)
    else:
        late_slope_pct = 0.0

    if lower_better:
        peak_value = min(smoothed_values)
        peak_step = steps[smoothed_values.index(peak_value)]
        peak_reversal_pct = 0.0 if is_scheduled else (smoothed_final - peak_value) / smoothed_range * 100
    else:
        peak_value = max(smoothed_values)
        peak_step = steps[smoothed_values.index(peak_value)]
        peak_reversal_pct = 0.0 if is_scheduled else (peak_value - smoothed_final) / smoothed_range * 100

    # Convergence and anomaly detection
    convergence = detect_convergence(data)
    anomalies = detect_anomalies(data)

    # Generate summary
    summary = _generate_summary(
        metric_name, trend, initial_value, final_value,
        improvement_percent, convergence, anomalies
    )

    # Raw sampled points (for debugging / high-detail view)
    sampled_points = smart_sample(data)

    # Smoothed points: EMA applied to full series, then smart-sampled
    smoothed_data = [(steps[i], smoothed_values[i], data[i][2]) for i in range(len(data))]
    smoothed_points = smart_sample(smoothed_data)

    return {
        'trend': trend,
        'recent_trend': recent_trend,
        'late_slope_pct': late_slope_pct,
        'peak_value': round(float(peak_value), 4),
        'peak_step': int(peak_step),
        'peak_reversal_pct': round(float(peak_reversal_pct), 1),
        'initial_value': float(initial_value),
        'final_value': float(final_value),
        'smoothed_final_value': round(float(smoothed_final), 4),
        'best_value': float(best_value),
        'best_step': int(best_step),
        'improvement_percent': float(improvement_percent),
        'ema_alpha': EMA_ALPHA,
        'converged': convergence['converged'],
        'convergence_step': convergence.get('step'),
        'anomaly_count': len(anomalies),
        'anomalies': anomalies,
        'summary': summary,
        'sampled_points': sampled_points,
        'smoothed_points': smoothed_points,
        'total_points': len(data)
    }


def detect_trend(values: list, metric_name: str) -> str:
    """
    Detect trend in values, ignoring initial spikes and being sensitive to RL noise.

    Returns: 'improving', 'stable', 'worsening', 'unstable', 'insufficient_data'
    """
    if len(values) < 10:
        return 'insufficient_data'

    # 1. Ignore "cold start" period (first 5% of data or first 20 points, whichever is smaller)
    # This prevents initial spikes from skewing the regression
    skip_count = min(20, len(values) // 20)
    v_clean = np.array(values[skip_count:])
    x_clean = np.arange(len(v_clean))

    if len(v_clean) < 5:
        return 'insufficient_data'

    # 2. Use a sliding window to smooth the data for regression (helps with noisy RL rewards)
    window_size = max(5, len(v_clean) // 10)
    v_smoothed = np.convolve(v_clean, np.ones(window_size)/window_size, mode='valid')
    x_smoothed = np.arange(len(v_smoothed))

    if len(v_smoothed) < 2:
        # Fallback to unsmoothed if windowing left us with too little data
        v_smoothed = v_clean
        x_smoothed = x_clean

    # 3. Linear regression on smoothed data
    coeffs = np.polyfit(x_smoothed, v_smoothed, 1)
    slope = coeffs[0]

    # Calculate R² on smoothed data
    y_pred = np.polyval(coeffs, x_smoothed)
    ss_res = np.sum((v_smoothed - y_pred) ** 2)
    ss_tot = np.sum((v_smoothed - np.mean(v_smoothed)) ** 2)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot != 0 else 0

    # 4. Significance test
    is_lower_better = _is_lower_better(metric_name)
    value_range = np.max(v_smoothed) - np.min(v_smoothed)
    
    if value_range > 0:
        # Check how much the linear trend explains vs the noise
        # For RL, a lower R² is acceptable if the slope is clearly in one direction
        normalized_slope = abs(slope * len(x_smoothed)) / (value_range if value_range != 0 else 1)
    else:
        normalized_slope = 0

    # Thresholds
    SLOPE_THRESHOLD = 0.05  # 5% change over the run is significant
    STABILITY_THRESHOLD = 0.4  # R² threshold for "consistent" trend

    if normalized_slope < SLOPE_THRESHOLD:
        return 'stable'

    # Scheduled metrics (epsilon, lr, etc.) are following a deliberate decay — label as stable
    if _is_scheduled_metric(metric_name):
        return 'stable'

    # Check if slope direction matches desired improvement
    if (is_lower_better and slope < 0) or (not is_lower_better and slope > 0):
        # Right direction with significant slope → improving (regardless of R²)
        return 'improving'
    else:
        # Wrong direction: distinguish consistent worsening from noisy instability
        if r_squared >= STABILITY_THRESHOLD:
            return 'worsening'
        return 'unstable'


def detect_convergence(data: list) -> dict:
    """
    Detect if metric has converged using a sliding window CV.
    """
    if len(data) < 20:
        return {'converged': False, 'step': None}

    values = [d[1] for d in data]
    
    # Check the very end of the run
    last_window = values[-max(10, len(values)//10):]
    mean_val = np.mean(last_window)
    std_val = np.std(last_window)
    
    cv = abs(std_val / mean_val) if abs(mean_val) > 1e-6 else std_val
    
    # RL rewards are noisy, so we use a more relaxed threshold for them
    is_reward = 'reward' in data[0] if isinstance(data[0], str) else False # Placeholder
    threshold = 0.10 if is_reward else 0.03
    
    if cv < threshold:
        # Try to find where it first converged
        # Use a sliding window of 10% of total length
        window_size = max(10, len(values) // 10)
        for i in range(window_size, len(values)):
            window = values[i-window_size:i]
            m = np.mean(window)
            s = np.std(window)
            curr_cv = abs(s / m) if abs(m) > 1e-6 else s
            if curr_cv < threshold:
                return {'converged': True, 'step': int(data[i][0])}

    return {'converged': False, 'step': None}


def detect_anomalies(data: list) -> list:
    """
    Detect anomalies, ignoring the first few points (cold start).
    """
    if len(data) < 20:
        return []

    # Ignore first 5% for anomaly detection bounds calculation
    skip = len(data) // 20
    values_clean = np.array([d[1] for d in data[skip:]])

    q1 = np.percentile(values_clean, 25)
    q3 = np.percentile(values_clean, 75)
    iqr = q3 - q1

    if iqr == 0:
        # If all values are same, check if any differ from the constant
        const_val = values_clean[0]
        anomalies = []
        for step, value, _ in data[skip:]:
            if abs(value - const_val) > 1e-6:
                anomalies.append({'step': int(step), 'value': float(value), 'reason': 'deviation'})
        return anomalies[:5]

    lower_bound = q1 - 2.5 * iqr # More conservative than 1.5
    upper_bound = q3 + 2.5 * iqr

    anomalies = []
    for step, value, _ in data[skip:]:
        if value > upper_bound:
            anomalies.append({'step': int(step), 'value': float(value), 'reason': 'spike_high'})
        elif value < lower_bound:
            anomalies.append({'step': int(step), 'value': float(value), 'reason': 'spike_low'})

    if anomalies:
        anomalies.sort(key=lambda x: abs(x['value']), reverse=True)
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
    metric_filter: list | None = None,
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

    # Evict stale cache entries that predate the smoothed_points feature
    if cached and not any(v.get('ema_alpha') for v in cached.values()):
        db.delete_metric_analyses(run_id)
        cached = None

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
            'recent_trend': analysis.get('recent_trend'),
            'initial_value': analysis['initial_value'],
            'final_value': analysis['final_value'],
            'smoothed_final_value': analysis.get('smoothed_final_value'),
            'best_value': analysis['best_value'],
            'best_step': analysis['best_step'],
            'peak_value': analysis.get('peak_value'),
            'peak_step': analysis.get('peak_step'),
            'peak_reversal_pct': analysis.get('peak_reversal_pct', 0.0),
            'ema_alpha': analysis.get('ema_alpha'),
            'improvement_percent': analysis['improvement_percent'],
            'converged': analysis['converged'],
            'convergence_step': analysis['convergence_step'],
            'anomaly_count': analysis['anomaly_count'],
            'anomalies': analysis['anomalies'],
            'summary': analysis['summary'],
            'total_points': analysis['total_points']
        }

        # Add smoothed + raw sampled points for medium/high detail
        if detail in ['medium', 'high']:
            metric_data['smoothed_points'] = analysis.get('smoothed_points', [])
            metric_data['sampled_points'] = analysis['sampled_points']

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


def _is_scheduled_metric(metric_name: str) -> bool:
    """Metrics that follow a deliberate schedule (e.g. epsilon decay) — neither better nor worse."""
    scheduled_keywords = ['epsilon', 'lr', 'learning_rate', 'temperature', 'alpha']
    metric_lower = metric_name.lower()
    return any(keyword in metric_lower for keyword in scheduled_keywords)


def _generate_summary(
    metric_name: str,
    trend: str,
    initial_value: float,
    final_value: float,
    improvement_percent: float,
    convergence: dict,
    anomalies: list
) -> str:
    """Generate human-readable summary with better qualitative analysis."""
    is_lower_better = _is_lower_better(metric_name)
    parts = []

    # Qualitative trend description
    if trend == 'improving':
        desc = "improving"
    elif trend == 'stable':
        desc = "stable"
    elif trend == 'worsening':
        desc = "worsening"
    elif trend == 'unstable':
        desc = "unstable"
    else:
        desc = "insufficient data"

    # Directional analysis (ignoring initial spikes for the text)
    if initial_value != 0:
        change = ((final_value - initial_value) / abs(initial_value)) * 100
        direction = "decreased" if change < 0 else "increased"
        parts.append(f"{metric_name}: {desc} (final: {final_value:.3g}, overall {direction} {abs(change):.1f}%)")
    else:
        parts.append(f"{metric_name}: {desc} (final: {final_value:.3g})")

    # Convergence status
    if convergence['converged']:
        parts.append(f"Converged at step {convergence['step']}")
    elif trend == 'stable':
        parts.append("Appears to have plateaued")

    # Anomaly count
    if anomalies:
        parts.append(f"{len(anomalies)} anomalies")

    return ". ".join(parts)


def cleanup_old_tb_logs(tb_logdir: str, keep_count: int, protected_dirs: set = None) -> dict:
    """
    Keep only the N most recent TensorBoard run directories.
    Directories in protected_dirs (e.g. from notable runs) are never deleted.

    Args:
        tb_logdir: Path to TensorBoard log directory (e.g., workspace/runs)
        keep_count: Number of recent non-protected runs to keep (0 = keep all)
        protected_dirs: Set of directory names that must not be deleted

    Returns:
        dict with 'deleted': list of deleted directory names, 'kept': list of kept directory names
    """
    import shutil

    log.info(f"cleanup_old_tb_logs called: tb_logdir={tb_logdir}, keep_count={keep_count}")

    if keep_count <= 0:
        return {'deleted': [], 'kept': [], 'message': 'No limit set, nothing deleted'}

    if not os.path.isdir(tb_logdir):
        log.warning(f"TensorBoard log directory not found: {tb_logdir}")
        return {'deleted': [], 'kept': [], 'message': 'TensorBoard log directory not found'}

    # Get all subdirectories and extract timestamps for sorting
    # Handles both Beekeeper format (YYYYMMDD-HHMMSS) and training script format (YYYY-MM-DD_HH-MM-SS_*)
    import re
    from datetime import datetime as dt

    def extract_timestamp(dirname):
        """Extract timestamp from directory name for sorting."""
        # Beekeeper format: 20260403-124635
        bk_match = re.match(r'^(\d{8})-(\d{6})$', dirname)
        if bk_match:
            try:
                return dt.strptime(f"{bk_match.group(1)}{bk_match.group(2)}", "%Y%m%d%H%M%S")
            except:
                pass

        # Training script format: 2026-04-03_12-46-35_tag
        ts_match = re.match(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', dirname)
        if ts_match:
            try:
                return dt.strptime(ts_match.group(1), "%Y-%m-%d_%H-%M-%S")
            except:
                pass

        # Fallback: use directory mtime
        return None

    subdirs = []
    for item in os.listdir(tb_logdir):
        path = os.path.join(tb_logdir, item)
        if os.path.isdir(path):
            timestamp = extract_timestamp(item)
            if timestamp:
                subdirs.append((item, path, timestamp))
                log.debug(f"Found TensorBoard dir: {item} -> {timestamp}")
            else:
                log.info(f"Skipping directory with unrecognized format: {item}")

    log.info(f"Found {len(subdirs)} subdirectories: {[name for name, _, _ in subdirs]}")

    if len(subdirs) <= keep_count:
        log.info(f"Only {len(subdirs)} runs found, nothing to delete (keep_count={keep_count})")
        return {
            'deleted': [],
            'kept': [name for name, _, _ in subdirs],
            'message': f'Only {len(subdirs)} run(s) found, nothing to delete'
        }

    # Sort by timestamp (newest first)
    subdirs.sort(key=lambda x: x[2], reverse=True)
    log.info(f"Sorted subdirs (newest first): {[(name, ts.strftime('%Y-%m-%d %H:%M:%S')) for name, _, ts in subdirs]}")

    # Separate protected dirs (notable runs) from candidates
    if protected_dirs:
        protected = [s for s in subdirs if s[0] in protected_dirs]
        candidates = [s for s in subdirs if s[0] not in protected_dirs]
    else:
        protected = []
        candidates = subdirs

    # Apply keep_count limit only to non-protected candidates
    to_keep = candidates[:keep_count] + protected
    to_delete = candidates[keep_count:]

    if protected:
        log.info(f"Protected (notable) TB dirs: {[name for name, _, _ in protected]}")
    log.info(f"Will KEEP ({len(to_keep)}): {[name for name, _, _ in to_keep]}")
    log.info(f"Will DELETE ({len(to_delete)}): {[name for name, _, _ in to_delete]}")

    deleted_names = []
    for name, path, _ in to_delete:
        try:
            log.info(f"Deleting directory: {path}")
            shutil.rmtree(path)
            deleted_names.append(name)
        except Exception as e:
            log.error(f"Failed to delete {name}: {e}")

    return {
        'deleted': deleted_names,
        'kept': [name for name, _, _ in to_keep],
        'message': f'Deleted {len(deleted_names)} old run(s), kept {len(to_keep)} recent run(s)'
    }


def list_tb_run_directories(tb_logdir: str) -> list:
    """
    List all TensorBoard run directories with their timestamps.

    Args:
        tb_logdir: Path to TensorBoard log directory

    Returns:
        List of dicts with 'name', 'path', and 'mtime' for each run directory
    """
    if not os.path.isdir(tb_logdir):
        return []

    runs = []
    for item in os.listdir(tb_logdir):
        path = os.path.join(tb_logdir, item)
        if os.path.isdir(path):
            stat = os.stat(path)
            runs.append({
                'name': item,
                'path': path,
                'mtime': stat.st_mtime
            })

    # Sort by name (timestamp) descending (newest first)
    runs.sort(key=lambda x: x['name'], reverse=True)
    return runs
