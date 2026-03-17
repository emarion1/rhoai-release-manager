#!/usr/bin/env python3
"""
Auto-scheduler for RHOAI Release Planning
Distributes features across release events based on priority and capacity
"""

def generate_release_schedule(start_version="3.5", num_releases=8):
    """
    Generate release schedule for next 2 years

    Args:
        start_version: Starting version (e.g., "3.5")
        num_releases: Number of releases to plan (8 = 2 years at quarterly cadence)

    Returns:
        List of release versions with event types
    """
    major, minor = map(int, start_version.split("."))

    schedule = []
    for i in range(num_releases):
        version = f"{major}.{minor + i}"
        schedule.append({
            "version": version,
            "events": ["EA1", "EA2", "GA"]
        })

    return schedule


def auto_schedule_features(features, capacity_guidelines, start_version="3.5", num_releases=8):
    """
    Auto-schedule features into release events based on priority

    Algorithm:
    1. Sort features by priority (rank from JIRA plan)
    2. Fill release events in order: 3.5 EA1, EA2, GA, 3.6 EA1, EA2, GA, etc.
    3. Respect capacity guidelines (target: typical_max, hard limit: aggressive_max)
    4. Balance features across events

    Args:
        features: List of feature dicts with 'rank', 'points', 'key', etc.
        capacity_guidelines: Dict with 'typical_max', 'aggressive_max', etc.
        start_version: Starting version
        num_releases: Number of releases to plan

    Returns:
        Dict mapping release events to feature lists
    """
    # Generate schedule
    schedule = generate_release_schedule(start_version, num_releases)

    # Initialize buckets
    plan = {}
    for release in schedule:
        version = release["version"]
        for event in release["events"]:
            plan[f"{version}-{event}"] = {
                "features": [],
                "points": 0,
                "capacity_status": "conservative"
            }

    # Sort features by:
    # 1. In plan first
    # 2. Target end date (earlier dates first, None last)
    # 3. Priority rank
    def sort_key(f):
        in_plan = f.get('in_plan', False)
        target_date = f.get('target_end_date', None)
        rank = f.get('rank', 9999)

        # Convert target_date to sortable value (None becomes very large)
        if target_date:
            date_sort = target_date  # ISO format sorts correctly
        else:
            date_sort = "9999-12-31"  # Far future for features without dates

        return (not in_plan, date_sort, rank)

    sorted_features = sorted(features, key=sort_key)

    # Capacity targets
    target_capacity = capacity_guidelines.get('typical_max', 50)
    max_capacity = capacity_guidelines.get('maximum', capacity_guidelines.get('aggressive_max', 80))

    # Distribute features
    bucket_keys = []
    for release in schedule:
        for event in release["events"]:
            bucket_keys.append(f"{release['version']}-{event}")

    current_bucket_idx = 0

    for feature in sorted_features:
        points = feature.get('points', 0)

        # Skip features with no points
        if points == 0:
            continue

        # Find next bucket that can fit this feature
        placed = False
        attempts = 0
        max_attempts = len(bucket_keys)

        while not placed and attempts < max_attempts:
            bucket_key = bucket_keys[current_bucket_idx % len(bucket_keys)]
            bucket = plan[bucket_key]

            # Check if feature fits in current bucket
            if bucket['points'] + points <= max_capacity:
                bucket['features'].append(feature)
                bucket['points'] += points

                # Update capacity status
                if bucket['points'] <= capacity_guidelines.get('conservative_max', 30):
                    bucket['capacity_status'] = 'conservative'
                elif bucket['points'] <= target_capacity:
                    bucket['capacity_status'] = 'typical'
                elif bucket['points'] <= capacity_guidelines.get('aggressive_max', 80):
                    bucket['capacity_status'] = 'aggressive'
                elif bucket['points'] <= max_capacity:
                    bucket['capacity_status'] = 'maximum'
                else:
                    bucket['capacity_status'] = 'over_capacity'

                placed = True

                # If bucket is near capacity, move to next bucket
                if bucket['points'] >= target_capacity:
                    current_bucket_idx += 1
            else:
                # Feature doesn't fit, try next bucket
                current_bucket_idx += 1
                attempts += 1

        # If not placed, leave it unscheduled (don't exceed capacity)
        # Features that don't fit by RHOAI 3.12 will remain unplanned

    return plan, schedule


def auto_schedule_features_enhanced(features, capacity_guidelines, start_version="3.5", num_releases=8, enable_splitting=True):
    """
    Enhanced auto-scheduler with XL feature splitting support.

    Features with points >= 13 are split into Part 1 (8 pts) + Part 2 (5 pts).
    Split parts get synthetic keys: {key}-P1, {key}-P2.

    Args:
        features: List of feature dicts
        capacity_guidelines: Dict with capacity thresholds
        start_version: Starting version
        num_releases: Number of releases to plan
        enable_splitting: Whether to apply XL splitting

    Returns:
        Dict with plan, schedule, splits_applied, mode
    """
    from copy import deepcopy

    processed_features = []
    splits_applied = 0

    for feature in features:
        points = feature.get('points', 0)

        if enable_splitting and points >= 13:
            # Split XL feature into P1 (8 pts) + P2 (5 pts)
            base_key = feature["key"]

            part1 = deepcopy(feature)
            part1["key"] = f"{base_key}-P1"
            part1["summary"] = f"{feature['summary'][:60]}... (Part 1: Core)"
            part1["points"] = 8
            part1["split"] = True
            part1["split_from"] = base_key
            part1["split_part"] = 1
            processed_features.append(part1)

            part2 = deepcopy(feature)
            part2["key"] = f"{base_key}-P2"
            part2["summary"] = f"{feature['summary'][:60]}... (Part 2: Extended)"
            part2["points"] = 5
            part2["split"] = True
            part2["split_from"] = base_key
            part2["split_part"] = 2
            processed_features.append(part2)

            splits_applied += 1
        else:
            feat_copy = deepcopy(feature)
            feat_copy["split"] = False
            feat_copy["split_from"] = None
            feat_copy["split_part"] = None
            processed_features.append(feat_copy)

    plan, schedule = auto_schedule_features(processed_features, capacity_guidelines, start_version, num_releases)

    return {
        "plan": plan,
        "schedule": schedule,
        "splits_applied": splits_applied,
        "mode": "optimized" if enable_splitting else "baseline"
    }


def format_plan_summary(plan, schedule):
    """Format auto-schedule plan for display"""
    summary = []

    for release in schedule:
        version = release["version"]
        release_total = 0
        release_features = 0

        summary.append(f"\n{'='*60}")
        summary.append(f"RHOAI-{version}")
        summary.append(f"{'='*60}")

        for event in release["events"]:
            bucket_key = f"{version}-{event}"
            bucket = plan[bucket_key]

            feature_count = len(bucket['features'])
            points = bucket['points']
            status = bucket['capacity_status']

            release_total += points
            release_features += feature_count

            status_icon = {
                'conservative': '🟢',
                'typical': '🟡',
                'aggressive': '🟠',
                'maximum': '🔴',
                'over_capacity': '🔴'
            }.get(status, '⚪')

            summary.append(f"\n  {event}: {feature_count} features, {points} pts {status_icon} ({status})")

            if feature_count > 0:
                # Show top 3 features
                for i, feat in enumerate(bucket['features'][:3]):
                    rank_str = f"#{feat['rank']}" if feat.get('in_plan') else "—"
                    summary.append(f"    {rank_str} {feat['key']} - {feat['summary'][:50]}... ({feat['points']} pts)")

                if feature_count > 3:
                    summary.append(f"    ... and {feature_count - 3} more features")

        summary.append(f"\n  TOTAL: {release_features} features, {release_total} pts")

    return "\n".join(summary)


if __name__ == "__main__":
    # Test with sample data
    sample_features = [
        {"key": "TEST-1", "summary": "High priority feature", "rank": 1, "points": 8, "in_plan": True},
        {"key": "TEST-2", "summary": "Medium priority", "rank": 2, "points": 5, "in_plan": True},
        {"key": "TEST-3", "summary": "Low priority", "rank": 3, "points": 13, "in_plan": True},
    ]

    capacity = {
        "conservative_max": 30,
        "typical_max": 50,
        "aggressive_max": 80
    }

    plan, schedule = auto_schedule_features(sample_features, capacity, start_version="3.5", num_releases=2)
    print(format_plan_summary(plan, schedule))
