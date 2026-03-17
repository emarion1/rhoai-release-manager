#!/usr/bin/env python3
"""
RHOAI Release Management Tool
Dual-mode: BUILD roadmaps + TRACK existing releases

Usage:
    export JIRA_TOKEN='your-token'
    python3 release_manager.py

Opens release-manager.html in browser
"""

import base64
import json
import os
import sys
import requests
from datetime import datetime
from collections import defaultdict

# Import auto-scheduler
try:
    from auto_scheduler import auto_schedule_features, auto_schedule_features_enhanced, format_plan_summary
except ImportError:
    # If running standalone, define inline
    def auto_schedule_features(features, capacity, start_version="3.5", num_releases=8):
        """Fallback: returns empty plan"""
        return {}, []
    def auto_schedule_features_enhanced(features, capacity, start_version="3.5", num_releases=8, enable_splitting=True):
        """Fallback: returns empty enhanced plan"""
        return {"plan": {}, "schedule": [], "splits_applied": 0, "mode": "baseline"}
    def format_plan_summary(plan, schedule):
        return "Auto-scheduler not available"

# Configuration
JIRA_BASE_URL = "https://redhat.atlassian.net"
JIRA_TOKEN = os.environ.get("JIRA_TOKEN")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL")
PROJECT = "RHAISTRAT"
PLAN_NAME = "RHOAI Feature Planning and Tracking"
PLAN_VIEW = "Outcomes & Features (Jeff's View)"

# JIRA Custom Fields (Atlassian Cloud IDs)
FIELD_STORY_POINTS = "customfield_10836"
FIELD_TARGET_VERSION = "customfield_10855"
FIELD_TARGET_END_DATE = "customfield_10015"  # Target end date for planning

# Capacity guidelines (from PREDICTIVE_RELEASE_CAPACITY_REPORT.md)
CAPACITY = {
    "median": 27.5,
    "mean": 38.7,
    "conservative_max": 30,
    "typical_max": 50,
    "aggressive_max": 80,
    "maximum": 140,
    "historical_max_release": 140,
}

# Feature sizing (from FEATURE_SIZING_QUICK_REFERENCE.md)
FEATURE_SIZING = {
    "XS": 1,
    "S": 3,
    "M": 5,
    "L": 8,
    "XL": 13
}


def get_jira_headers():
    """Get JIRA API headers"""
    if not JIRA_TOKEN or not JIRA_EMAIL:
        print("❌ ERROR: JIRA_TOKEN and/or JIRA_EMAIL environment variable not set")
        print("\nSet your Atlassian Cloud API token and email:")
        print("  export JIRA_EMAIL='your-email@redhat.com'")
        print("  export JIRA_TOKEN='your-api-token'")
        print("\nGet API token from: https://id.atlassian.com/manage-profile/security/api-tokens")
        sys.exit(1)

    credentials = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/json"
    }


def get_jira_plan_id():
    """Get plan ID for 'RHOAI Feature Planning and Tracking'"""
    print(f"🔍 Searching for JIRA Plan: '{PLAN_NAME}'...")

    # Try Advanced Roadmaps API endpoints
    endpoints = [
        "/rest/jpo/1.0/plan",
        "/rest/portfolio/1.0/plan",
        "/rest/teams/1.0/plan/search"
    ]

    for endpoint in endpoints:
        try:
            response = requests.get(
                f"{JIRA_BASE_URL}{endpoint}",
                headers=get_jira_headers(),
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()
                print(f"✅ Found plans via {endpoint}")

                # Search for matching plan name
                plans = data if isinstance(data, list) else data.get("values", [])
                for plan in plans:
                    if PLAN_NAME.lower() in plan.get("title", "").lower():
                        plan_id = plan.get("id")
                        print(f"✅ Found plan ID: {plan_id}")
                        return plan_id

        except Exception as e:
            print(f"  ⚠️ {endpoint} not accessible: {e}")
            continue

    print(f"⚠️  Could not find plan via API")
    return None


def get_plan_feature_ranking(plan_id):
    """Get feature ranking from JIRA Plan"""
    if not plan_id:
        return {}

    print(f"📊 Fetching feature ranking from plan {plan_id}...")

    try:
        # Try to get issues from plan in ranked order
        response = requests.get(
            f"{JIRA_BASE_URL}/rest/jpo/1.0/plan/{plan_id}/issue",
            headers=get_jira_headers(),
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            issues = data if isinstance(data, list) else data.get("issues", [])

            # Create ranking dict: {issue_key: rank_number}
            ranking = {}
            for idx, issue in enumerate(issues, start=1):
                key = issue.get("key") or issue.get("issueKey")
                if key:
                    ranking[key] = idx

            print(f"✅ Retrieved ranking for {len(ranking)} features")
            return ranking

    except Exception as e:
        print(f"⚠️  Could not get plan ranking: {e}")

    return {}


def get_all_features():
    """Get all RHAISTRAT features with status, points, versions"""
    print(f"📥 Querying all {PROJECT} features...")

    jql = f"project = {PROJECT} AND type IN (Feature, Initiative, Epic, Story)"

    all_issues = []
    max_results = 100
    next_page_token = None

    while True:
        params = {
            "jql": jql,
            "fields": f"key,summary,status,priority,issuetype,{FIELD_STORY_POINTS},fixVersions,{FIELD_TARGET_VERSION},{FIELD_TARGET_END_DATE},labels,issuelinks",
            "maxResults": max_results
        }
        if next_page_token:
            params["nextPageToken"] = next_page_token

        response = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=get_jira_headers(),
            params=params,
            timeout=30
        )

        if response.status_code != 200:
            print(f"❌ JIRA query failed: {response.status_code}")
            return []

        data = response.json()
        issues = data.get("issues", [])
        all_issues.extend(issues)

        print(f"  Retrieved {len(all_issues)} features so far...")

        # Token-based pagination: stop when isLast or no nextPageToken
        if data.get("isLast", True) or "nextPageToken" not in data:
            break
        next_page_token = data["nextPageToken"]

    print(f"✅ Retrieved {len(all_issues)} total features")
    return all_issues


def estimate_feature_size(summary, priority):
    """
    Auto-estimate feature size based on summary and priority
    Returns story points (3, 5, 8, or 13)
    """
    summary_lower = summary.lower()

    # Keywords indicating size
    xl_keywords = ["infrastructure", "migration", "integration", "architecture", "redesign", "framework"]
    l_keywords = ["implement", "develop", "create", "build", "support", "enable"]
    m_keywords = ["update", "enhance", "improve", "add", "extend"]
    s_keywords = ["fix", "adjust", "minor", "small", "ui", "ux", "docs"]

    # Check for XL indicators
    if any(kw in summary_lower for kw in xl_keywords) or priority == "Blocker":
        return FEATURE_SIZING["XL"]  # 13

    # Check for L indicators
    if any(kw in summary_lower for kw in l_keywords) or priority == "Critical":
        return FEATURE_SIZING["L"]  # 8

    # Check for S indicators
    if any(kw in summary_lower for kw in s_keywords):
        return FEATURE_SIZING["S"]  # 3

    # Default to Medium
    return FEATURE_SIZING["M"]  # 5


def parse_features(issues, ranking):
    """Parse JIRA issues into feature objects"""
    features = []
    auto_sized_count = 0

    for idx, issue in enumerate(issues):
        key = issue["key"]
        fields = issue["fields"]

        # Parse fix versions (committed releases) — only RHOAI versions
        fix_versions = []
        for fv in fields.get("fixVersions", []):
            name = fv["name"]
            if name.lower().startswith("rhoai"):
                fix_versions.append(name)

        # Parse target version (planned release) — only RHOAI versions
        target_version = None
        if fields.get(FIELD_TARGET_VERSION):
            tv_field = fields[FIELD_TARGET_VERSION]
            # Handle both dict and list formats
            if isinstance(tv_field, dict):
                name = tv_field.get("name", "")
                if name.lower().startswith("rhoai"):
                    target_version = name
            elif isinstance(tv_field, list) and len(tv_field) > 0:
                # Find first RHOAI version in the list
                for tv in tv_field:
                    name = tv.get("name", "") if isinstance(tv, dict) else str(tv)
                    if name.lower().startswith("rhoai"):
                        target_version = name
                        break
            else:
                name = str(tv_field) if tv_field else ""
                if name.lower().startswith("rhoai"):
                    target_version = name

        # Parse target end date
        target_end_date = None
        if fields.get(FIELD_TARGET_END_DATE):
            target_end_date = fields[FIELD_TARGET_END_DATE]  # Format: "YYYY-MM-DD"

        # Determine scheduling status
        if fix_versions:
            scheduled_to = fix_versions[0]
            status_category = "committed"
        elif target_version:
            scheduled_to = target_version
            status_category = "planned"
        else:
            scheduled_to = None
            status_category = "unscheduled"

        # Get labels
        labels = fields.get("labels", [])

        # Get story points - AUTO-SIZE if 0 or missing
        points = fields.get(FIELD_STORY_POINTS) or 0
        original_points = points
        sizing_method = "jira_provided"
        complexity_score = None
        sizing_confidence = None

        if points == 0:
            priority = fields["priority"]["name"] if fields.get("priority") else "Normal"
            points = estimate_feature_size(fields["summary"], priority)
            sizing_method = "keyword_heuristic"
            auto_sized_count += 1

        # Check if feature is in the plan
        in_plan = key in ranking
        rank = ranking.get(key, 9999)  # Use plan ranking or default to end

        # Get issue type
        issue_type = fields.get("issuetype", {}).get("name", "Feature")

        feature = {
            "key": key,
            "summary": fields["summary"],
            "status": fields["status"]["name"],
            "priority": fields["priority"]["name"] if fields.get("priority") else "Normal",
            "issue_type": issue_type,
            "points": points,
            "original_points": original_points,
            "auto_sized": points != original_points,
            "sizing_method": sizing_method,
            "complexity_score": complexity_score,
            "sizing_confidence": sizing_confidence,
            "fix_versions": fix_versions,
            "target_version": target_version,
            "target_end_date": target_end_date,
            "scheduled_to": scheduled_to,
            "status_category": status_category,
            "labels": labels,
            "rank": rank,
            "in_plan": in_plan,
        }

        features.append(feature)

    # Sort by: in_plan first (True before False), then by rank
    features.sort(key=lambda f: (not f["in_plan"], f["rank"]))

    print(f"  🤖 Auto-sized {auto_sized_count} features with missing story points")

    return features


def group_features_by_release(features):
    """Group features by scheduled release"""
    releases = defaultdict(lambda: {
        "EA1": [],
        "EA2": [],
        "GA": []
    })

    unscheduled = []

    for feature in features:
        scheduled_to = feature["scheduled_to"]

        if not scheduled_to:
            unscheduled.append(feature)
            continue

        scheduled_lower = scheduled_to.lower().replace("rhoai-", "").replace("_", " ")

        # Parse release version (e.g., "rhoai-3.4.EA1" -> release="3.4", event="EA1")
        # Handle formats like: "3.4", "3.4.EA1", "3.4-EA1", "3.4 EA1", "rhaiis-3.4 ea-1"

        # Extract version number (e.g., "3.4" or "2.20")
        import re
        version_match = re.search(r'(\d+)\.(\d+)', scheduled_to)

        if version_match:
            major = version_match.group(1)
            minor = version_match.group(2)
            release_num = f"{major}.{minor}"

            # Determine event type
            if "ea1" in scheduled_lower or "ea-1" in scheduled_lower:
                event = "EA1"
            elif "ea2" in scheduled_lower or "ea-2" in scheduled_lower:
                event = "EA2"
            elif "ga" in scheduled_lower:
                event = "GA"
            else:
                # No event specified - default to GA
                event = "GA"

            releases[release_num][event].append(feature)
        else:
            # Couldn't parse version - add to unscheduled
            unscheduled.append(feature)

    return dict(releases), unscheduled


def calculate_release_metrics(release_data):
    """Calculate metrics for each release event"""
    metrics = {}

    for event, features in release_data.items():
        total_points = sum(f["points"] for f in features)
        total_features = len(features)

        # Capacity status
        if total_points <= CAPACITY["conservative_max"]:
            capacity_status = "conservative"
            color = "#28a745"  # green
        elif total_points <= CAPACITY["typical_max"]:
            capacity_status = "typical"
            color = "#90ee90"  # light green
        elif total_points <= CAPACITY["aggressive_max"]:
            capacity_status = "aggressive"
            color = "#ffc107"  # yellow
        elif total_points <= CAPACITY.get("maximum", 140):
            capacity_status = "maximum"
            color = "#dc3545"  # red
        else:
            capacity_status = "over_capacity"
            color = "#dc3545"  # red

        vs_median = round((total_points / CAPACITY["median"] - 1) * 100) if total_points > 0 else 0

        metrics[event] = {
            "features": total_features,
            "points": total_points,
            "capacity_status": capacity_status,
            "color": color,
            "vs_median_pct": vs_median
        }

    return metrics


def analyze_feature_phasing(feature):
    """
    Analyze if a feature can be phased across DP/TP/GA
    Returns: {
        "phaseable": bool,
        "recommendation": str,
        "complexity": str
    }
    """
    summary = feature["summary"].lower()
    points = feature["points"]

    # Indicators that feature is large/complex enough to phase
    phase_indicators = [
        "infrastructure", "architecture", "framework", "integration",
        "migration", "platform", "ecosystem", "redesign"
    ]

    # Indicators that feature is atomic (shouldn't be phased)
    atomic_indicators = [
        "fix", "bug", "typo", "documentation", "docs",
        "minor", "small", "adjust", "update config"
    ]

    has_phase_indicator = any(ind in summary for ind in phase_indicators)
    has_atomic_indicator = any(ind in summary for ind in atomic_indicators)

    # Determine if phaseable
    if points >= 8 and has_phase_indicator:
        return {
            "phaseable": True,
            "recommendation": f"Split into phases: DP (basic functionality) → TP (extended features) → GA (production hardening)",
            "complexity": "High"
        }
    elif points >= 8 and not has_atomic_indicator:
        return {
            "phaseable": True,
            "recommendation": f"Consider phasing: DP (core feature) → TP (refinement) → GA (optimization)",
            "complexity": "Medium"
        }
    elif points >= 5 and has_phase_indicator:
        return {
            "phaseable": True,
            "recommendation": f"Potential for DP/TP split, GA for full release",
            "complexity": "Medium"
        }
    elif has_atomic_indicator or points <= 3:
        return {
            "phaseable": False,
            "recommendation": "Deliver as single feature (not complex enough to phase)",
            "complexity": "Low"
        }
    else:
        return {
            "phaseable": False,
            "recommendation": "Deliver in single release event (GA preferred)",
            "complexity": "Low"
        }


def generate_split_recommendation(feature):
    """
    Generate specific splitting recommendations for oversized features
    Returns detailed split strategy or None if feature shouldn't be split
    """
    summary = feature["summary"]
    summary_lower = summary.lower()
    points = feature["points"]

    # Check if feature contains multiple concerns
    has_multiple = any(word in summary_lower for word in ["and", "multiple", "several", "various", "&"])

    if not has_multiple and points < 13:
        return None

    # Analyze what type of split would work best
    split_details = []

    # Infrastructure/Integration features
    if any(word in summary_lower for word in ["infrastructure", "integration", "platform"]):
        split_details = [
            {"name": f"Part 1: Core Infrastructure", "points": 8, "phase": "DP"},
            {"name": f"Part 2: Integration & Testing", "points": 5, "phase": "TP/GA"}
        ]
        reason = "Complex infrastructure work - split into core setup and integration phases"
        suggested_split = "8 pts (core) + 5 pts (integration)"

    # Architecture/Redesign features
    elif any(word in summary_lower for word in ["architecture", "redesign", "refactor"]):
        split_details = [
            {"name": f"Part 1: Design & Foundation", "points": 5, "phase": "DP"},
            {"name": f"Part 2: Implementation", "points": 8, "phase": "TP/GA"}
        ]
        reason = "Architectural work - separate design phase from implementation"
        suggested_split = "5 pts (design) + 8 pts (implementation)"

    # Migration features
    elif "migration" in summary_lower:
        split_details = [
            {"name": f"Part 1: Migration Framework", "points": 5, "phase": "DP"},
            {"name": f"Part 2: Data Migration", "points": 5, "phase": "TP"},
            {"name": f"Part 3: Validation & Cleanup", "points": 3, "phase": "GA"}
        ]
        reason = "Migration complexity - split into framework, execution, and validation"
        suggested_split = "5 pts + 5 pts + 3 pts"

    # Features with "and" - likely multiple concerns
    elif " and " in summary_lower or " & " in summary_lower:
        # Try to identify the two parts
        parts = summary.replace(" and ", "|").replace(" & ", "|").split("|")
        if len(parts) >= 2:
            split_details = [
                {"name": f"Part 1: {parts[0].strip()[:50]}", "points": 8, "phase": "DP/TP"},
                {"name": f"Part 2: {parts[1].strip()[:50]}", "points": 5, "phase": "TP/GA"}
            ]
            reason = "Feature contains multiple concerns - split into separate deliverables"
            suggested_split = "8 pts (first part) + 5 pts (second part)"
        else:
            split_details = [
                {"name": f"Part 1: Core Functionality", "points": 8, "phase": "DP/TP"},
                {"name": f"Part 2: Extended Features", "points": 5, "phase": "GA"}
            ]
            reason = "Large scope - split into core and extended functionality"
            suggested_split = "8 pts (core) + 5 pts (extended)"

    # Default split for other large features
    else:
        split_details = [
            {"name": f"Part 1: Core Functionality", "points": 8, "phase": "DP/TP"},
            {"name": f"Part 2: Refinement & Optimization", "points": 5, "phase": "GA"}
        ]
        reason = "Large feature - split into MVP and refinement phases"
        suggested_split = "8 pts (MVP) + 5 pts (refinement)"

    return {
        "reason": reason,
        "suggested_split": suggested_split,
        "split_details": split_details
    }


def analyze_feature_sizing(features):
    """
    Analyze feature sizing distribution and provide recommendations
    Returns: {
        "distribution": {...},
        "recommendations": [...],
        "oversized": [...],
        "undersized": [...]
    }
    """
    distribution = {
        "XL": {"count": 0, "features": [], "total_points": 0},
        "L": {"count": 0, "features": [], "total_points": 0},
        "M": {"count": 0, "features": [], "total_points": 0},
        "S": {"count": 0, "features": [], "total_points": 0},
        "XS": {"count": 0, "features": [], "total_points": 0}
    }

    total_features = len(features)
    total_points = 0
    oversized = []  # Features that should be split
    undersized = []  # Features that might be combined

    for feature in features:
        points = feature["points"]
        total_points += points

        # Categorize by size
        if points >= 13:
            size = "XL"
        elif points >= 8:
            size = "L"
        elif points >= 5:
            size = "M"
        elif points >= 3:
            size = "S"
        else:
            size = "XS"

        distribution[size]["count"] += 1
        distribution[size]["features"].append(feature)
        distribution[size]["total_points"] += points

        # Identify oversized features (XL that could be split)
        if points >= 13:
            summary_lower = feature["summary"].lower()
            split_recommendation = generate_split_recommendation(feature)

            if split_recommendation:
                oversized.append({
                    "feature": feature,
                    "reason": split_recommendation["reason"],
                    "suggested_split": split_recommendation["suggested_split"],
                    "split_details": split_recommendation["split_details"]
                })

    # Calculate percentages
    for size in distribution:
        distribution[size]["percentage"] = round((distribution[size]["count"] / total_features * 100), 1) if total_features > 0 else 0

    # Generate recommendations
    recommendations = []

    # Check for too many XL features
    xl_pct = distribution["XL"]["percentage"]
    if xl_pct > 15:
        recommendations.append({
            "type": "warning",
            "message": f"{xl_pct}% of features are XL (13 pts). Ideal is <10%. Consider splitting large features into smaller, deliverable increments.",
            "impact": "high"
        })

    # Check for too many M features
    m_pct = distribution["M"]["percentage"]
    if m_pct > 50:
        recommendations.append({
            "type": "info",
            "message": f"{m_pct}% of features are M (5 pts). This is acceptable but consider if some could be S (3 pts) for faster delivery.",
            "impact": "low"
        })

    # Check for good L distribution
    l_pct = distribution["L"]["percentage"]
    if l_pct > 60:
        recommendations.append({
            "type": "warning",
            "message": f"{l_pct}% of features are L (8 pts). High proportion of large features. Consider breaking down into M or S.",
            "impact": "medium"
        })

    # Ideal distribution recommendation
    recommendations.append({
        "type": "success",
        "message": f"Ideal distribution: S(35%), M(40%), L(20%), XL(<5%). Current: S({distribution['S']['percentage']}%), M({distribution['M']['percentage']}%), L({distribution['L']['percentage']}%), XL({distribution['XL']['percentage']}%)",
        "impact": "info"
    })

    return {
        "distribution": distribution,
        "total_features": total_features,
        "total_points": total_points,
        "recommendations": recommendations,
        "oversized": oversized,
        "average_size": round(total_points / total_features, 1) if total_features > 0 else 0
    }


def generate_optimized_plan(features, capacity, sizing_analysis):
    """
    Generate optimized release plan based on sizing recommendations
    Applies feature splitting recommendations and re-schedules
    """
    from copy import deepcopy

    # Create optimized feature list
    optimized_features = []

    for feature in features:
        # Check if feature should be split
        should_split = any(
            rec["feature"]["key"] == feature["key"]
            for rec in sizing_analysis["oversized"]
        )

        if should_split and feature["points"] >= 13:
            # Split XL feature into 2 L features
            base_key = feature["key"]

            # Part 1: Core functionality
            part1 = deepcopy(feature)
            part1["key"] = f"{base_key}-P1"
            part1["summary"] = f"{feature['summary'][:60]}... (Part 1: Core)"
            part1["points"] = 8
            part1["auto_sized"] = False
            part1["optimized"] = True
            optimized_features.append(part1)

            # Part 2: Extended functionality
            part2 = deepcopy(feature)
            part2["key"] = f"{base_key}-P2"
            part2["summary"] = f"{feature['summary'][:60]}... (Part 2: Extended)"
            part2["points"] = 5
            part2["auto_sized"] = False
            part2["optimized"] = True
            optimized_features.append(part2)
        else:
            # Keep feature as-is
            optimized_features.append(feature)

    # Re-run auto-scheduler on optimized features
    from auto_scheduler import auto_schedule_features

    optimized_plan, schedule = auto_schedule_features(
        optimized_features,
        capacity,
        start_version="3.5",
        num_releases=8
    )

    return {
        "plan": optimized_plan,
        "schedule": schedule,
        "features": optimized_features,
        "split_count": len(optimized_features) - len(features)
    }


def analyze_backlog(features):
    """
    Comprehensive backlog analysis
    Returns complete analysis for HTML display
    """
    # 1. Phasing analysis
    phasing_results = []
    phaseable_count = 0

    for feature in features:
        result = analyze_feature_phasing(feature)
        if result["phaseable"]:
            phaseable_count += 1
        phasing_results.append({
            "feature": feature,
            "analysis": result
        })

    # 2. Sizing analysis
    sizing_analysis = analyze_feature_sizing(features)

    # 3. Optimization insights
    insights = {
        "phasing": {
            "total": len(features),
            "phaseable": phaseable_count,
            "percentage": round((phaseable_count / len(features) * 100), 1) if features else 0
        },
        "sizing": sizing_analysis,
        "efficiency_score": calculate_efficiency_score(sizing_analysis)
    }

    return {
        "phasing_results": phasing_results,
        "sizing_analysis": sizing_analysis,
        "insights": insights
    }


def calculate_efficiency_score(sizing_analysis):
    """Calculate delivery efficiency score based on sizing distribution"""
    dist = sizing_analysis["distribution"]

    # Ideal distribution weights
    # Higher score for more S and M features (faster delivery)
    score = 0
    score += dist["S"]["percentage"] * 1.5  # S features get 1.5x weight
    score += dist["M"]["percentage"] * 1.2  # M features get 1.2x weight
    score += dist["L"]["percentage"] * 0.8  # L features get 0.8x weight
    score += dist["XL"]["percentage"] * 0.5  # XL features get 0.5x weight (should be rare)

    # Cap at 100
    return min(100, round(score))


def build_plan_data(recommended_plan, optimized_plan):
    """Build unified planData dict for JS embedding."""

    def bucket_to_js(bucket_data, object_format=False):
        """Convert a plan bucket to JS-friendly format."""
        if object_format:
            features_js = []
            for f in bucket_data["features"]:
                entry = {
                    "key": f["key"],
                    "points": f["points"],
                    "split": f.get("split", False),
                }
                if f.get("split"):
                    entry["split_from"] = f.get("split_from", "")
                    entry["split_part"] = f.get("split_part")
                features_js.append(entry)
        else:
            features_js = [{"key": f["key"], "points": f["points"], "split": False} for f in bucket_data["features"]]
        return {
            "features": features_js,
            "points": bucket_data["points"],
            "capacity_status": bucket_data["capacity_status"],
        }

    baseline = {}
    if recommended_plan:
        for k, v in recommended_plan.items():
            baseline[k] = bucket_to_js(v, object_format=False)

    optimized = {}
    splits_applied = 0
    if optimized_plan and optimized_plan.get("plan"):
        for k, v in optimized_plan["plan"].items():
            optimized[k] = bucket_to_js(v, object_format=True)
        splits_applied = optimized_plan.get("splits_applied", optimized_plan.get("split_count", 0))

    return {
        "baseline": baseline,
        "optimized": optimized,
        "metadata": {"splits_applied": splits_applied},
    }


def generate_html(features, releases, unscheduled, capacity, recommended_plan=None, backlog_analysis=None, optimized_plan=None):
    """Generate interactive HTML release manager"""

    # Count features in vs not in plan (before using in HTML)
    in_plan_count = sum(1 for f in unscheduled if f['in_plan'])
    not_in_plan_count = len(unscheduled) - in_plan_count

    # Calculate metrics for all releases
    release_metrics = {}
    for release_num, release_data in releases.items():
        release_metrics[release_num] = calculate_release_metrics(release_data)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>RHOAI Release Manager</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: #f5f7fa;
            color: #333;
        }}

        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 20px 30px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}

        .header h1 {{
            font-size: 24px;
            margin-bottom: 5px;
        }}

        .header p {{
            opacity: 0.9;
            font-size: 14px;
        }}

        .tabs {{
            background: white;
            border-bottom: 2px solid #e1e8ed;
            padding: 0 30px;
        }}

        .tab-button {{
            display: inline-block;
            padding: 15px 25px;
            cursor: pointer;
            border: none;
            background: none;
            font-size: 15px;
            font-weight: 500;
            color: #666;
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
        }}

        .tab-button:hover {{
            color: #667eea;
        }}

        .tab-button.active {{
            color: #667eea;
            border-bottom-color: #667eea;
        }}

        .tab-content {{
            display: none;
            padding: 30px;
        }}

        .tab-content.active {{
            display: block;
        }}

        .planning-layout {{
            display: grid;
            grid-template-columns: 300px 1fr;
            gap: 20px;
            height: calc(100vh - 200px);
        }}

        .feature-pool {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow-y: auto;
        }}

        .feature-pool h2 {{
            font-size: 16px;
            margin-bottom: 15px;
            color: #333;
        }}

        .feature-card {{
            background: #f8f9fa;
            border: 1px solid #dee2e6;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 10px;
            cursor: move;
            transition: all 0.2s;
        }}

        .feature-card:hover {{
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            transform: translateY(-2px);
        }}

        .feature-card.dragging {{
            opacity: 0.5;
        }}

        .feature-key {{
            font-weight: 600;
            color: #0052cc;
            font-size: 13px;
            margin-bottom: 4px;
        }}

        .feature-summary {{
            font-size: 13px;
            color: #333;
            margin-bottom: 8px;
            line-height: 1.4;
        }}

        .feature-meta {{
            display: flex;
            gap: 10px;
            font-size: 11px;
            color: #666;
        }}

        .feature-points {{
            background: #e3f2fd;
            padding: 2px 6px;
            border-radius: 3px;
            font-weight: 500;
        }}

        .feature-rank {{
            background: #fff3cd;
            padding: 2px 6px;
            border-radius: 3px;
        }}

        .releases-area {{
            overflow-y: auto;
        }}

        .release-section {{
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .release-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }}

        .release-title {{
            font-size: 18px;
            font-weight: 600;
        }}

        .release-events {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 15px;
        }}

        .event-bucket {{
            background: #f8f9fa;
            border: 2px dashed #dee2e6;
            border-radius: 6px;
            padding: 15px;
            min-height: 200px;
        }}

        .event-bucket.drag-over {{
            background: #e7f3ff;
            border-color: #667eea;
        }}

        .event-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 2px solid #dee2e6;
        }}

        .event-name {{
            font-weight: 600;
            font-size: 14px;
        }}

        .capacity-meter {{
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 12px;
        }}

        .capacity-bar {{
            width: 60px;
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
        }}

        .capacity-fill {{
            height: 100%;
            transition: all 0.3s;
        }}

        .tracking-layout {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        .release-selector {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .release-selector select {{
            padding: 10px 15px;
            font-size: 15px;
            border: 2px solid #dee2e6;
            border-radius: 6px;
            cursor: pointer;
        }}

        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 20px;
        }}

        .metric-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .metric-label {{
            font-size: 12px;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }}

        .metric-value {{
            font-size: 28px;
            font-weight: 600;
            color: #333;
        }}

        .metric-subtitle {{
            font-size: 12px;
            color: #999;
            margin-top: 4px;
        }}

        .alert {{
            padding: 15px;
            border-radius: 6px;
            margin-bottom: 20px;
        }}

        .alert-info {{
            background: #d1ecf1;
            border-left: 4px solid #0c5460;
            color: #0c5460;
        }}

        .alert-warning {{
            background: #fff3cd;
            border-left: 4px solid #856404;
            color: #856404;
        }}

        .alert-danger {{
            background: #f8d7da;
            border-left: 4px solid #721c24;
            color: #721c24;
        }}

        .feature-table {{
            width: 100%;
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .feature-table th {{
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-size: 12px;
            text-transform: uppercase;
            color: #666;
            border-bottom: 2px solid #dee2e6;
        }}

        .feature-table td {{
            padding: 12px;
            border-bottom: 1px solid #dee2e6;
            font-size: 13px;
        }}

        .feature-table tr:hover {{
            background: #f8f9fa;
        }}

        .status-badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
        }}

        .status-new {{ background: #e7e7e7; color: #333; }}
        .status-progress {{ background: #0052cc; color: white; }}
        .status-review {{ background: #ff991f; color: white; }}
        .status-pending {{ background: #00875a; color: white; }}
        .status-closed {{ background: #36b37e; color: white; }}

        .priority-blocker {{ color: #de350b; font-weight: 600; }}
        .priority-critical {{ color: #ff5630; font-weight: 600; }}
        .priority-major {{ color: #ff8b00; }}
        .priority-normal {{ color: #666; }}

        .info-icon {{
            display: inline-block;
            width: 18px;
            height: 18px;
            background: #0052cc;
            color: white;
            border-radius: 50%;
            text-align: center;
            line-height: 18px;
            font-size: 12px;
            cursor: pointer;
            margin-left: 5px;
        }}

        .info-icon:hover {{
            background: #0065ff;
        }}

        .modal {{
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.5);
        }}

        .modal-content {{
            background: white;
            margin: 5% auto;
            padding: 30px;
            border-radius: 10px;
            width: 80%;
            max-width: 700px;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }}

        .modal-close {{
            color: #aaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
        }}

        .modal-close:hover {{
            color: #000;
        }}

        .info-card {{
            background: #e7f3ff;
            border-left: 4px solid #0052cc;
            padding: 15px;
            margin: 15px 0;
            border-radius: 4px;
        }}

        .info-card h4 {{
            margin: 0 0 8px 0;
            color: #0052cc;
        }}

        .info-card p {{
            margin: 5px 0;
            font-size: 14px;
            line-height: 1.5;
        }}

        .analysis-nav-btn {{
            padding: 10px 20px;
            margin: 0 10px 0 0;
            border: none;
            background: #f5f5f5;
            color: #666;
            cursor: pointer;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            transition: all 0.3s;
        }}

        .analysis-nav-btn:hover {{
            background: #e0e0e0;
        }}

        .analysis-nav-btn.active {{
            background: #667eea;
            color: white;
        }}

        .analysis-section {{
            background: white;
            padding: 25px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .metric-box {{
            display: inline-block;
            padding: 15px 20px;
            margin: 10px;
            border-radius: 8px;
            text-align: center;
            min-width: 150px;
        }}

        .metric-box-value {{
            font-size: 32px;
            font-weight: 700;
            margin-bottom: 5px;
        }}

        .metric-box-label {{
            font-size: 12px;
            color: #666;
            text-transform: uppercase;
        }}

        .recommendation-box {{
            padding: 15px;
            margin: 10px 0;
            border-left: 4px solid;
            border-radius: 4px;
        }}

        .recommendation-high {{
            background: #fff5f5;
            border-color: #dc3545;
        }}

        .recommendation-medium {{
            background: #fff8e6;
            border-color: #ffc107;
        }}

        .recommendation-low {{
            background: #f0f7ff;
            border-color: #0052cc;
        }}

        .recommendation-success {{
            background: #f0fff4;
            border-color: #28a745;
        }}

        /* Capacity slider */
        .capacity-slider-container {{
            background: white;
            padding: 20px 25px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }}

        .capacity-slider {{
            width: 100%;
            height: 8px;
            -webkit-appearance: none;
            appearance: none;
            border-radius: 4px;
            outline: none;
            margin: 15px 0;
        }}

        .capacity-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            appearance: none;
            width: 22px;
            height: 22px;
            border-radius: 50%;
            background: #667eea;
            cursor: pointer;
            border: 2px solid white;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
        }}

        .slider-labels {{
            font-size: 11px;
            color: #666;
            height: 24px;
        }}

        .slider-labels span {{
            padding: 3px 8px;
            border-radius: 4px;
            white-space: nowrap;
        }}

        /* Plan mode toggle */
        .plan-mode-selector {{
            display: inline-flex;
            background: #f0f0f0;
            border-radius: 8px;
            padding: 3px;
            margin-left: 20px;
        }}

        .mode-btn {{
            padding: 8px 16px;
            border: none;
            background: transparent;
            cursor: pointer;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            color: #666;
            transition: all 0.2s;
        }}

        .mode-btn:hover {{
            color: #333;
        }}

        .mode-btn.active {{
            background: #667eea;
            color: white;
            box-shadow: 0 2px 4px rgba(102,126,234,0.3);
        }}

        .mode-badge {{
            display: inline-block;
            background: #ff8b00;
            color: white;
            font-size: 10px;
            padding: 1px 6px;
            border-radius: 8px;
            margin-left: 4px;
            vertical-align: middle;
        }}

        /* Sizing method badges */
        .sizing-badge {{
            display: inline-block;
            padding: 1px 5px;
            border-radius: 3px;
            font-size: 9px;
            font-weight: 600;
            margin-left: 4px;
            vertical-align: middle;
        }}

        .sizing-badge-jira {{
            background: #e3fcef;
            color: #006644;
        }}

        .sizing-badge-ai {{
            background: #deebff;
            color: #0747a6;
        }}

        .sizing-badge-kw {{
            background: #fff3cd;
            color: #856404;
        }}

        /* Complexity score display */
        .complexity-score {{
            display: inline-block;
            font-size: 10px;
            font-weight: 600;
            padding: 1px 5px;
            border-radius: 3px;
            margin-left: 4px;
        }}

        .complexity-low {{ background: #e3fcef; color: #006644; }}
        .complexity-medium {{ background: #fff3cd; color: #856404; }}
        .complexity-high {{ background: #fff5f5; color: #de350b; }}

        /* Split indicator */
        .split-indicator {{
            display: inline-block;
            background: #e7f3ff;
            color: #0052cc;
            font-size: 10px;
            font-weight: 600;
            padding: 2px 6px;
            border-radius: 3px;
            margin-left: 4px;
        }}

        /* Over-capacity dimming */
        .feature-dimmed {{
            opacity: 0.35;
            filter: grayscale(50%);
            transition: opacity 0.3s, filter 0.3s;
        }}

        .event-over-capacity {{
            border-left-color: #dc3545 !important;
            background: #fff5f5 !important;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>RHOAI Release Manager</h1>
        <p>Build roadmaps and track release progress | Data from JIRA Plan: {PLAN_NAME}</p>
    </div>

    <div class="tabs">
        <button class="tab-button active" onclick="switchTab('tracking')">📊 Track Current Release Cycles</button>
        <button class="tab-button" onclick="switchTab('drafts')">📝 Draft Release Plans</button>
        <button class="tab-button" onclick="switchTab('analysis')">🔬 Feature Analysis</button>
        <button class="tab-button" onclick="showHelp()" style="margin-left:auto;background:#f8f9fa;color:#333;">❓ Help</button>
    </div>

    <!-- TRACKING TAB (DEFAULT) -->
    <div id="tracking-tab" class="tab-content active">
        <div class="tracking-layout">
            <div class="release-selector">
                <label for="release-select"><strong>Select Release Cycle to Track:</strong>
                    <span class="info-icon" onclick="showInfo('release-cycle')">ℹ️</span>
                </label>
                <select id="release-select" onchange="loadRelease(this.value)">
                    <option value="">-- Select Release Cycle --</option>
"""

    # Add existing releases to dropdown
    for release_num in sorted(releases.keys(), reverse=True):
        html += f'                    <option value="{release_num}">RHOAI-{release_num}</option>\n'

    html += """
                </select>
                <p style="font-size:12px;color:#666;margin-top:8px;">
                    Each release cycle includes 3 events: EA1, EA2, and GA
                </p>
            </div>

            <div id="release-details">
                <div class="alert alert-info">
                    <strong>👆 Select a release cycle above</strong> to view tracking details for all 3 events (EA1, EA2, GA)
                </div>
            </div>
        </div>
    </div>

    <!-- DRAFT PLANS TAB -->
    <div id="drafts-tab" class="tab-content">
        <div style="max-width: 1400px; margin: 0 auto;">
            <div class="alert alert-info" style="display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                <div>
                    <strong>📝 AI-Recommended 2-Year Release Plan</strong>
                    <span class="info-icon" onclick="showInfo('draft-plan')">ℹ️</span>
                    <p style="margin-top: 10px; font-size: 14px;">
                        Auto-scheduled by priority ranking, story points, and capacity limits.
                    </p>
                </div>
                <div class="plan-mode-selector">
                    <button class="mode-btn active" id="mode-btn-baseline" onclick="setPlanMode('baseline')">Baseline</button>
                    <button class="mode-btn" id="mode-btn-optimized" onclick="setPlanMode('optimized')">
                        Optimized (XL Split)
                        <span class="mode-badge" id="split-count-badge"></span>
                    </button>
                </div>
            </div>

            <!-- Capacity slider -->
            <div class="capacity-slider-container">
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <strong style="font-size: 14px;">Capacity Limit per Event</strong>
                    <span id="capacity-value" style="font-size: 18px; font-weight: 700; color: #667eea;">80 pts</span>
                    <span id="capacity-risk" style="font-size: 13px; font-weight: 500; padding: 3px 10px; border-radius: 4px; background: #fff3cd; color: #856404;">Aggressive</span>
                </div>
                <input type="range" class="capacity-slider" id="capacity-slider" min="30" max="140" value="80" step="5"
                    oninput="updateCapacitySlider(this.value)">
                <div style="position: relative; height: 10px; margin: 0 6px; border-radius: 5px; background: linear-gradient(to right, #28a745 0%, #28a745 0.9%, #ffc107 0.9%, #ffc107 18.2%, #ff8b00 18.2%, #ff8b00 45.5%, #dc3545 45.5%, #dc3545 100%);">
                </div>
                <div class="slider-labels" style="position: relative; margin-top: 8px;">
                    <span style="position: absolute; left: 0%; background: #e3fcef; color: #006644;">≤30 Conservative</span>
                    <span style="position: absolute; left: 18.2%; transform: translateX(-50%); background: #fff3cd; color: #856404;">31-50 Typical</span>
                    <span style="position: absolute; left: 45.5%; transform: translateX(-50%); background: #fff0e6; color: #c45100;">51-80 Aggressive</span>
                    <span style="position: absolute; right: 0%; background: #fff5f5; color: #de350b;">81-140 Maximum</span>
                </div>
            </div>

            <div id="draft-plan-display">
                <!-- Populated by JavaScript -->
            </div>
        </div>
    </div>

    <!-- FEATURE ANALYSIS TAB -->
    <div id="analysis-tab" class="tab-content">
        <div style="max-width: 1400px; margin: 0 auto;">
            <div class="alert alert-info">
                <strong>🔬 Feature Backlog Analysis & Optimization</strong>
                <span class="info-icon" onclick="showInfo('analysis')">ℹ️</span>
                <p style="margin-top: 10px; font-size: 14px;">
                    Comprehensive analysis of your feature backlog for optimal delivery:
                    <br>• Feature sizing distribution and optimization
                    <br>• Delivery efficiency scoring and recommendations
                    <br>• XL feature splits are auto-applied in the Draft Plans tab (Optimized mode)
                </p>
            </div>

            <!-- Navigation within Analysis tab -->
            <div style="background: white; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                <button onclick="showAnalysisSection('sizing')" class="analysis-nav-btn active" id="btn-sizing">
                    📏 Sizing Analysis
                </button>
                <button onclick="showAnalysisSection('recommendations')" class="analysis-nav-btn" id="btn-recommendations">
                    💡 Recommendations
                </button>
            </div>

            <!-- Section 1: Sizing Analysis -->
            <div id="analysis-sizing" class="analysis-section">
                <!-- Populated by JavaScript -->
            </div>

            <!-- Section 2: Recommendations -->
            <div id="analysis-recommendations" class="analysis-section" style="display:none;">
                <!-- Populated by JavaScript -->
            </div>
        </div>
    </div>

    <script>
        // Store all data
        const jiraBaseUrl = """ + json.dumps(JIRA_BASE_URL) + """;

        function getJiraSearchUrl(keys) {
            const jql = 'key in (' + keys.join(', ') + ')';
            return jiraBaseUrl + '/issues/?jql=' + encodeURIComponent(jql);
        }

        const allReleases = """ + json.dumps(releases, indent=2) + """;
        const releaseMetrics = """ + json.dumps(release_metrics, indent=2) + """;
        const capacity = """ + json.dumps(capacity, indent=2) + """;
        // Backlog analysis data
        const backlogAnalysis = """ + json.dumps(backlog_analysis if backlog_analysis else {}, indent=2) + """;

        // Full feature lookup for getting names and details
        const allFeatures = """ + json.dumps({f["key"]: {
            "summary": f["summary"],
            "points": f["points"],
            "product": f.get("product", "RHOAI"),
            "issue_type": f.get("issue_type", "Feature"),
            "status": f.get("status", ""),
            "priority": f.get("priority", "Normal"),
            "auto_sized": f.get("auto_sized", False),
            "sizing_method": f.get("sizing_method", "jira_provided"),
            "complexity_score": f.get("complexity_score"),
            "sizing_confidence": f.get("sizing_confidence"),
        } for f in features}, indent=2) + """;

        // Unified plan data: baseline + optimized
        const planData = """ + json.dumps(build_plan_data(recommended_plan, optimized_plan), indent=2) + """;

        // Feature lookup for quick access
        const featureElements = {};

        document.addEventListener('DOMContentLoaded', function() {
            // Build feature element lookup
            document.querySelectorAll('.feature-card').forEach(card => {
                featureElements[card.dataset.key] = card;
            });
        });

        // Tab switching
        function switchTab(tab) {
            document.querySelectorAll('.tab-button').forEach(btn => btn.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

            event.target.classList.add('active');
            document.getElementById(tab + '-tab').classList.add('active');
        }

        // Load release tracking details
        function loadRelease(releaseNum) {
            if (!releaseNum) {
                document.getElementById('release-details').innerHTML = `
                    <div class="alert alert-info">Select a release above to view tracking details</div>
                `;
                return;
            }

            const releaseData = allReleases[releaseNum];
            const metrics = releaseMetrics[releaseNum];

            if (!releaseData) {
                document.getElementById('release-details').innerHTML = `
                    <div class="alert alert-warning">No data found for RHOAI-${releaseNum}</div>
                `;
                return;
            }

            // Calculate totals
            let totalFeatures = 0;
            let totalPoints = 0;
            for (const event in releaseData) {
                totalFeatures += metrics[event].features;
                totalPoints += metrics[event].points;
            }

            const vsHistorical = (totalPoints / capacity.historical_max_release * 100).toFixed(0);

            let html = `
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-label">Total Features</div>
                        <div class="metric-value">${totalFeatures}</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">Total Points</div>
                        <div class="metric-value">${totalPoints}</div>
                        <div class="metric-subtitle">${vsHistorical}% of historical max</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">EA1</div>
                        <div class="metric-value">${metrics.EA1.points}</div>
                        <div class="metric-subtitle">${metrics.EA1.features} features (${metrics.EA1.vs_median_pct > 0 ? '+' : ''}${metrics.EA1.vs_median_pct}%)</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">EA2</div>
                        <div class="metric-value">${metrics.EA2.points}</div>
                        <div class="metric-subtitle">${metrics.EA2.features} features (${metrics.EA2.vs_median_pct > 0 ? '+' : ''}${metrics.EA2.vs_median_pct}%)</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-label">GA</div>
                        <div class="metric-value">${metrics.GA.points}</div>
                        <div class="metric-subtitle">${metrics.GA.features} features (${metrics.GA.vs_median_pct > 0 ? '+' : ''}${metrics.GA.vs_median_pct}%)</div>
                    </div>
                </div>
            `;

            // Add capacity warnings
            if (totalPoints > capacity.aggressive_max) {
                html += `
                    <div class="alert alert-danger">
                        <strong>⚠️ Over Capacity:</strong> ${totalPoints} points exceeds aggressive threshold (${capacity.aggressive_max} pts).
                        This release is ${vsHistorical}% of historical maximum. Consider descoping.
                    </div>
                `;
            } else if (totalPoints > capacity.typical_max) {
                html += `
                    <div class="alert alert-warning">
                        <strong>⚠️ Aggressive Scope:</strong> ${totalPoints} points is above typical capacity (${capacity.typical_max} pts).
                        Requires strong execution and may need mitigations.
                    </div>
                `;
            }

            html += `
                <div style="background: #f0f7ff; border-left: 4px solid #667eea; padding: 8px 14px; margin-bottom: 10px; border-radius: 4px; font-size: 13px; color: #555;">
                    Click any event heading to open its features in JIRA.
                </div>
            `;

            for (const event of ['EA1', 'EA2', 'GA']) {
                const features = releaseData[event];
                if (features && features.length > 0) {
                    html += `
                        <div class="metric-card" style="grid-column: 1 / -1; margin-top: 10px;">
                            <h3 style="margin-bottom: 15px;"><a href="${getJiraSearchUrl(features.map(f => f.key))}" target="_blank" style="color: inherit; text-decoration: none; border-bottom: 1px dashed #667eea;">${event} Features (${features.length})</a></h3>
                            <table class="feature-table">
                                <thead>
                                    <tr>
                                        <th>Key</th>
                                        <th>Summary</th>
                                        <th>Status</th>
                                        <th>Priority</th>
                                        <th>Points</th>
                                    </tr>
                                </thead>
                                <tbody>
                    `;

                    features.forEach(f => {
                        const statusClass = 'status-' + f.status.toLowerCase().replace(/[^a-z]/g, '');
                        const priorityClass = 'priority-' + f.priority.toLowerCase();
                        const typeBadge = f.issue_type === 'Initiative'
                            ? '<span style="display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;background:#e3fcef;color:#006644;margin-left:6px;">Initiative</span>'
                            : '<span style="display:inline-block;font-size:11px;padding:1px 6px;border-radius:3px;background:#deebff;color:#0747a6;margin-left:6px;">Feature</span>';
                        html += `
                                    <tr>
                                        <td><a href="${jiraBaseUrl}/browse/${f.key}" target="_blank">${f.key}</a>${typeBadge}</td>
                                        <td>${f.summary}</td>
                                        <td><span class="status-badge ${statusClass}">${f.status}</span></td>
                                        <td class="${priorityClass}">${f.priority}</td>
                                        <td><strong>${f.points}</strong></td>
                                    </tr>
                        `;
                    });

                    html += `
                                </tbody>
                            </table>
                        </div>
                    `;
                }
            }

            document.getElementById('release-details').innerHTML = html;
        }

        // Help and info functions
        function showHelp() {
            document.getElementById('help-modal').style.display = 'block';
        }

        function showInfo(topic) {
            const infoContent = {
                'release-cycle': `
                    <h3>About Release Cycles</h3>
                    <div class="info-card">
                        <h4>What is a Release Cycle?</h4>
                        <p>Each RHOAI release (e.g., 3.4, 3.5) is a quarterly release cycle with 3 distinct events:</p>
                        <ul>
                            <li><strong>EA1 (Early Access 1):</strong> First preview release</li>
                            <li><strong>EA2 (Early Access 2):</strong> Second preview release</li>
                            <li><strong>GA (General Availability):</strong> Production-ready release</li>
                        </ul>
                    </div>
                    <div class="info-card">
                        <h4>Fix Version vs Target Version</h4>
                        <p><strong>Fix Version:</strong> Feature is <em>committed</em> and approved for this release event</p>
                        <p><strong>Target Version:</strong> Feature is <em>intended</em> for this release event but not yet committed</p>
                    </div>
                    <div class="info-card">
                        <h4>Default to GA</h4>
                        <p>Features with only a version number (e.g., "3.5") but no specific event (EA1/EA2) default to the GA release event.</p>
                    </div>
                `,
                'capacity': `
                    <h3>Capacity Guidelines</h3>
                    <div class="info-card">
                        <h4>Story Points per Event</h4>
                        <ul>
                            <li>🟢 <strong>Conservative:</strong> ≤30 pts - Low risk</li>
                            <li>🟡 <strong>Typical:</strong> 31-50 pts - Normal capacity</li>
                            <li>🟠 <strong>Aggressive:</strong> 51-80 pts - High load, needs mitigations</li>
                            <li>🔴 <strong>Maximum:</strong> 81-140 pts - Historical ceiling (90% CI)</li>
                        </ul>
                        <p><strong>Historical baseline:</strong> Median 27.5 pts/event, Max 140 pts per entire release</p>
                    </div>
                `,
                'draft-plan': `
                    <h3>About Draft Release Plans</h3>
                    <div class="info-card">
                        <h4>How the AI Scheduler Works</h4>
                        <p>The auto-scheduler distributes unscheduled features across future releases using:</p>
                        <ul>
                            <li><strong>Priority First:</strong> Features ranked in JIRA Plan are scheduled before others</li>
                            <li><strong>Capacity Aware:</strong> Targets ~50 pts per event (typical capacity)</li>
                            <li><strong>Hard Limits:</strong> Up to 140 pts per event (historical max within 90% CI)</li>
                            <li><strong>Sequential Fill:</strong> Fills 3.5 EA1 → EA2 → GA → 3.6 EA1 → etc.</li>
                        </ul>
                    </div>
                    <div class="info-card">
                        <h4>Plan Modes</h4>
                        <ul>
                            <li><strong>Baseline:</strong> Original features, no splitting</li>
                            <li><strong>Optimized (XL Split):</strong> XL features (13+ pts) split into Part 1 (8 pts) + Part 2 (5 pts)</li>
                        </ul>
                    </div>
                    <div class="info-card">
                        <h4>Capacity Slider</h4>
                        <p>Adjust the per-event capacity limit to explore risk tolerance:</p>
                        <ul>
                            <li>🟢 ≤30 pts: Conservative</li>
                            <li>🟡 31-50 pts: Typical</li>
                            <li>🟠 51-80 pts: Aggressive</li>
                            <li>🔴 81-140 pts: Maximum (historical limit)</li>
                        </ul>
                        <p>Features exceeding the selected limit are dimmed in the plan view.</p>
                    </div>
                    <div class="info-card">
                        <h4>Sizing Badges</h4>
                        <ul>
                            <li><strong style="color:#006644;">●</strong> JIRA-provided sizing</li>
                            <li><strong style="color:#0747a6;">AI</strong> Complexity scoring (auto-sized)</li>
                            <li><strong style="color:#856404;">KW</strong> Keyword heuristic (auto-sized)</li>
                        </ul>
                    </div>
                `,
                'analysis': `
                    <h3>About Feature Analysis</h3>
                    <div class="info-card">
                        <h4>Sizing Distribution Analysis</h4>
                        <p>Shows breakdown of features by size (XS/S/M/L/XL) and compares to ideal distribution:</p>
                        <ul>
                            <li>S (3 pts): 35% - Fast delivery</li>
                            <li>M (5 pts): 40% - Balanced value</li>
                            <li>L (8 pts): 20% - Acceptable if necessary</li>
                            <li>XL (13 pts): &lt;5% - Should be rare, consider splitting</li>
                        </ul>
                        <p><strong>Efficiency Score:</strong> Weighted score based on distribution (target: 80+)</p>
                    </div>
                    <div class="info-card">
                        <h4>Recommendations</h4>
                        <p>Specific suggestions for improving delivery efficiency:</p>
                        <ul>
                            <li>Which XL features should be split into smaller deliverables</li>
                            <li>How current distribution compares to ideal targets</li>
                            <li>Expected benefits of optimization (faster delivery, better planning)</li>
                        </ul>
                        <p>XL splits are auto-applied in the Draft Plans tab under Optimized mode.</p>
                    </div>
                `
            };

            if (infoContent[topic]) {
                document.getElementById('info-modal-content').innerHTML = infoContent[topic];
                document.getElementById('info-modal').style.display = 'block';
            }
        }

        // Current plan mode and capacity limit
        let currentPlanMode = 'baseline';
        let currentCapacityLimit = 80;

        // Set plan mode (baseline or optimized)
        function setPlanMode(mode) {
            currentPlanMode = mode;
            document.querySelectorAll('.mode-btn').forEach(btn => btn.classList.remove('active'));
            document.getElementById('mode-btn-' + mode).classList.add('active');
            renderDraftPlan();
        }

        // Update capacity slider display and re-render
        function updateCapacitySlider(value) {
            currentCapacityLimit = parseInt(value);
            const valEl = document.getElementById('capacity-value');
            const riskEl = document.getElementById('capacity-risk');
            const slider = document.getElementById('capacity-slider');

            valEl.textContent = value + ' pts';

            let label, bgColor, textColor, sliderBg;
            if (value <= 30) {
                label = 'Conservative'; bgColor = '#e3fcef'; textColor = '#006644'; sliderBg = '#28a745';
            } else if (value <= 50) {
                label = 'Typical'; bgColor = '#fff3cd'; textColor = '#856404'; sliderBg = '#ffc107';
            } else if (value <= 80) {
                label = 'Aggressive'; bgColor = '#fff0e6'; textColor = '#c45100'; sliderBg = '#ff8b00';
            } else {
                label = 'Maximum'; bgColor = '#fff5f5'; textColor = '#de350b'; sliderBg = '#dc3545';
            }
            riskEl.textContent = label;
            riskEl.style.background = bgColor;
            riskEl.style.color = textColor;
            slider.style.background = `linear-gradient(to right, ${sliderBg} 0%, ${sliderBg} ${(value - 30) / 110 * 100}%, #e0e0e0 ${(value - 30) / 110 * 100}%, #e0e0e0 100%)`;

            renderDraftPlan();
        }

        // Get sizing method badge HTML
        function getSizingBadge(feature) {
            if (!feature) return '';
            const method = feature.sizing_method || 'jira_provided';
            if (method === 'complexity_scoring') return '<span class="sizing-badge sizing-badge-ai">AI</span>';
            if (method === 'keyword_heuristic') return '<span class="sizing-badge sizing-badge-kw">KW</span>';
            return '<span class="sizing-badge sizing-badge-jira">●</span>';
        }

        // Get complexity score HTML
        function getComplexityBadge(feature) {
            if (!feature || feature.complexity_score == null) return '';
            const score = feature.complexity_score;
            const cls = score <= 4 ? 'complexity-low' : score <= 8 ? 'complexity-medium' : 'complexity-high';
            return `<span class="complexity-score ${cls}">${score}</span>`;
        }

        // Get confidence label HTML
        function getConfidenceBadge(feature) {
            if (!feature || !feature.sizing_confidence) return '';
            return `<span style="font-size:9px;color:#999;margin-left:3px;">${feature.sizing_confidence}</span>`;
        }

        // Render draft plan
        function renderDraftPlan() {
            const container = document.getElementById('draft-plan-display');
            const activePlan = planData[currentPlanMode] || {};

            // Update split count badge
            const badge = document.getElementById('split-count-badge');
            if (planData.metadata && planData.metadata.splits_applied > 0) {
                badge.textContent = planData.metadata.splits_applied + ' splits';
                badge.style.display = 'inline-block';
            } else {
                badge.style.display = 'none';
            }

            if (!activePlan || Object.keys(activePlan).length === 0) {
                container.innerHTML = `
                    <div class="alert alert-warning">
                        <strong>⚠️ No draft plan available</strong>
                        <p>The auto-scheduler was unable to generate a recommended plan for this mode.</p>
                    </div>
                `;
                return;
            }

            const quarters = {
                "3.5": "Q2 2026", "3.6": "Q3 2026", "3.7": "Q4 2026", "3.8": "Q1 2027",
                "3.9": "Q2 2027", "3.10": "Q3 2027", "3.11": "Q4 2027", "3.12": "Q1 2028"
            };

            const releaseGoals = {
                "3.5": "Focus on distributed inference improvements, model serving enhancements, and evaluation capabilities.",
                "3.6": "Advance observability and showback features, API parity improvements, and agent metadata support.",
                "3.7": "Enhance AI safety tools, Kubeflow migration support, and model catalog customization.",
                "3.8": "Strengthen agentic framework support, Ray training improvements, and MLflow integration.",
                "3.9": "Expand RBAC capabilities, AutoML integration, and vLLM CPU support for broader deployment.",
                "3.10": "Deepen multilingual support, MCP server integration, and OIDC authentication across components.",
                "3.11": "Advance FIPS compliance, data science pipeline UX, and inference graph capabilities.",
                "3.12": "Refine IDE integration, Feature Store RBAC, and lifecycle documentation for enterprise readiness."
            };

            // Group buckets by release
            const releases = {};
            for (const bucketKey in activePlan) {
                const parts = bucketKey.split('-');
                const version = parts[0];
                const event = parts[1];
                if (!releases[version]) {
                    releases[version] = { EA1: null, EA2: null, GA: null };
                }
                releases[version][event] = activePlan[bucketKey];
            }

            let html = `
                <div style="background: #f0f7ff; border-left: 4px solid #667eea; padding: 8px 14px; margin-bottom: 15px; border-radius: 4px; font-size: 13px; color: #555;">
                    Click any event heading (EA1, EA2, GA) to open its features in JIRA.
                </div>
            `;
            const sortedVersions = Object.keys(releases)
                .filter(v => parseFloat(v) >= 3.4)
                .sort((a, b) => parseFloat(a) - parseFloat(b));

            for (const version of sortedVersions) {
                const releaseData = releases[version];
                const quarter = quarters[version] || '';

                let releaseTotalFeatures = 0;
                let releaseTotalPoints = 0;
                for (const event in releaseData) {
                    if (releaseData[event]) {
                        releaseTotalFeatures += releaseData[event].features.length;
                        releaseTotalPoints += releaseData[event].points;
                    }
                }

                const goals = releaseGoals[version] || 'Planned feature delivery for this release cycle.';

                html += `
                    <div style="background: white; border-radius: 8px; padding: 25px; margin-bottom: 25px; box-shadow: 0 2px 8px rgba(0,0,0,0.1);">
                        <h2 style="margin: 0 0 10px 0; color: #333; border-bottom: 2px solid #667eea; padding-bottom: 10px;">
                            RHOAI-${version}
                            <span style="font-size: 16px; color: #666; font-weight: normal;">(${quarter})</span>
                            <span style="float: right; font-size: 16px; font-weight: normal; color: #666;">
                                ${releaseTotalFeatures} features, ${releaseTotalPoints} pts total
                            </span>
                        </h2>
                        <div style="background: #f0f7ff; border-left: 4px solid #667eea; padding: 12px 15px; margin: 0 0 20px 0; border-radius: 4px;">
                            <strong style="color: #667eea; font-size: 14px;">Release Goals:</strong>
                            <p style="margin: 5px 0 0 0; color: #555; font-size: 14px; line-height: 1.5;">${goals}</p>
                        </div>
                        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px;">
                `;

                for (const event of ['EA1', 'EA2', 'GA']) {
                    const eventData = releaseData[event];

                    if (eventData && eventData.features.length > 0) {
                        const overCapacity = eventData.points > currentCapacityLimit;

                        const statusIcons = {
                            'conservative': '🟢', 'typical': '🟡', 'aggressive': '🟠',
                            'maximum': '🔴', 'over_capacity': '🔴'
                        };
                        const statusColors = {
                            'conservative': '#28a745', 'typical': '#90ee90', 'aggressive': '#ffc107',
                            'maximum': '#dc3545', 'over_capacity': '#dc3545'
                        };

                        let displayStatus = eventData.capacity_status;
                        let statusColor = statusColors[displayStatus] || '#ccc';
                        let statusIcon = statusIcons[displayStatus] || '⚪';

                        // Override display if over the slider capacity limit
                        if (overCapacity) {
                            statusColor = '#dc3545';
                            statusIcon = '🔴';
                        }

                        const eventClass = overCapacity ? 'event-over-capacity' : '';

                        const realKeys = [...new Set(eventData.features.map(f => {
                            if (typeof f === 'object') return f.split_from || f.key;
                            return f;
                        }))];

                        html += `
                            <div class="${eventClass}" style="background: #f8f9fa; border-radius: 6px; padding: 15px; border-left: 4px solid ${statusColor};">
                                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #333;">
                                    <a href="${getJiraSearchUrl(realKeys)}" target="_blank" style="color: inherit; text-decoration: none; border-bottom: 1px dashed #667eea;">${event} ${statusIcon}</a>
                                    ${overCapacity ? '<span style="font-size:11px;color:#dc3545;font-weight:normal;"> Over limit</span>' : ''}
                                </h3>
                                <p style="margin: 0 0 10px 0; font-size: 14px; color: #666;">
                                    <strong>${eventData.features.length} features, ${eventData.points} pts</strong>
                                    <br>
                                    <em style="font-size: 12px;">${displayStatus.replace('_', ' ')}</em>
                                </p>
                        `;

                        // Size distribution
                        const sizeDistribution = { XL: 0, L: 0, M: 0, S: 0, XS: 0 };
                        eventData.features.forEach(f => {
                            const pts = typeof f === 'object' ? f.points : (allFeatures[f] ? allFeatures[f].points : 0);
                            if (pts >= 13) sizeDistribution.XL++;
                            else if (pts >= 8) sizeDistribution.L++;
                            else if (pts >= 5) sizeDistribution.M++;
                            else if (pts >= 3) sizeDistribution.S++;
                            else sizeDistribution.XS++;
                        });

                        html += `<div style="background: white; padding: 8px; border-radius: 4px; margin-bottom: 10px; font-size: 11px;"><strong>Sizes:</strong>`;
                        ['XL', 'L', 'M', 'S', 'XS'].forEach(size => {
                            if (sizeDistribution[size] > 0) {
                                html += ` <span style="background: #e0e0e0; padding: 2px 5px; border-radius: 2px; margin: 0 2px;">${size}:${sizeDistribution[size]}</span>`;
                            }
                        });
                        html += `</div>`;

                        // Feature list - handle both object and string-key formats
                        html += `<div style="max-height: 200px; overflow-y: auto;">`;

                        // Track cumulative points for dimming
                        let cumulativePoints = 0;
                        eventData.features.forEach(f => {
                            let key, pts, isSplit = false, splitFrom = null, splitPart = null;

                            if (typeof f === 'object') {
                                key = f.key;
                                pts = f.points;
                                isSplit = f.split || false;
                                splitFrom = f.split_from || null;
                                splitPart = f.split_part || null;
                            } else {
                                key = f;
                                const feat = allFeatures[key];
                                pts = feat ? feat.points : 0;
                            }

                            cumulativePoints += pts;
                            const dimmed = cumulativePoints > currentCapacityLimit;
                            const dimClass = dimmed ? 'feature-dimmed' : '';

                            // Look up feature details (use split_from for split features)
                            const lookupKey = splitFrom || key;
                            const feature = allFeatures[lookupKey] || allFeatures[key];
                            const size = pts >= 13 ? 'XL' : pts >= 8 ? 'L' : pts >= 5 ? 'M' : pts >= 3 ? 'S' : 'XS';

                            html += `<div class="${dimClass}" style="padding: 6px 0; border-bottom: 1px solid #eee; font-size: 11px;">`;
                            html += `<div style="font-weight: 600; color: #0052cc; margin-bottom: 2px;">`;
                            html += `<a href="${jiraBaseUrl}/browse/${lookupKey}" target="_blank" style="color: #0052cc; text-decoration: none;">${key}</a>`;
                            html += ` <span style="background: #667eea; color: white; padding: 1px 4px; border-radius: 2px; font-size: 10px; margin-left: 4px;">${pts}pts ${size}</span>`;

                            // Sizing method badge
                            if (feature) html += getSizingBadge(feature);

                            // Complexity score (for auto-sized features)
                            if (feature) html += getComplexityBadge(feature);

                            // Confidence label
                            if (feature) html += getConfidenceBadge(feature);

                            // Split indicator
                            if (isSplit && splitPart) {
                                html += `<span class="split-indicator">Part ${splitPart}/2</span>`;
                            }

                            html += `</div>`;
                            if (feature) {
                                const summary = feature.summary || '';
                                html += `<div style="color: #666; font-size: 10px;">${summary.substring(0, 80)}${summary.length > 80 ? '...' : ''}</div>`;
                            }
                            html += `</div>`;
                        });
                        html += `</div></div>`;
                    } else {
                        html += `
                            <div style="background: #f8f9fa; border-radius: 6px; padding: 15px; border-left: 4px solid #ccc;">
                                <h3 style="margin: 0 0 10px 0; font-size: 16px; color: #999;">${event}</h3>
                                <p style="margin: 0; font-size: 14px; color: #999;"><em>No features scheduled</em></p>
                            </div>
                        `;
                    }
                }

                html += `</div></div>`;
            }

            container.innerHTML = html;
        }

        // Initialize on load
        document.addEventListener('DOMContentLoaded', function() {
            // Set default plan mode to optimized if splits available
            if (planData.metadata && planData.metadata.splits_applied > 0) {
                setPlanMode('optimized');
            } else {
                renderDraftPlan();
            }
            // Initialize slider visual
            updateCapacitySlider(80);
            renderAnalysis();
        });

        // Analysis tab navigation
        function showAnalysisSection(section) {
            // Hide all sections
            document.querySelectorAll('.analysis-section').forEach(s => s.style.display = 'none');
            document.querySelectorAll('.analysis-nav-btn').forEach(btn => btn.classList.remove('active'));

            // Show selected section
            document.getElementById(`analysis-${section}`).style.display = 'block';
            document.getElementById(`btn-${section}`).classList.add('active');
        }

        // Render all analysis sections
        function renderAnalysis() {
            if (!backlogAnalysis || !backlogAnalysis.insights) {
                document.getElementById('analysis-sizing').innerHTML = '<p>No analysis data available</p>';
                return;
            }

            renderSizingAnalysis();
            renderRecommendations();
        }

        // Render Sizing Analysis section
        function renderSizingAnalysis() {
            const container = document.getElementById('analysis-sizing');
            const sizing = backlogAnalysis.sizing_analysis;
            const dist = sizing.distribution;

            let html = `
                <h2 style="margin: 0 0 20px 0; color: #333;">Feature Sizing Distribution</h2>

                <div style="margin: 20px 0;">
                    <div class="metric-box" style="background: #e7f3ff;">
                        <div class="metric-box-value" style="color: #0052cc;">${sizing.total_features}</div>
                        <div class="metric-box-label">Total Features</div>
                    </div>
                    <div class="metric-box" style="background: #fff8e6;">
                        <div class="metric-box-value" style="color: #ff8b00;">${sizing.average_size}</div>
                        <div class="metric-box-label">Average Size (pts)</div>
                    </div>
                    <div class="metric-box" style="background: #f0fff4;">
                        <div class="metric-box-value" style="color: #28a745;">${backlogAnalysis.insights.efficiency_score}</div>
                        <div class="metric-box-label">Efficiency Score</div>
                    </div>
                </div>

                <h3 style="margin: 20px 0 15px 0; color: #333;">Distribution by Size</h3>
                <table class="feature-table">
                    <thead>
                        <tr>
                            <th>Size</th>
                            <th>Points</th>
                            <th>Count</th>
                            <th>Percentage</th>
                            <th>Total Points</th>
                            <th>Ideal Target</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            const idealTargets = {
                "XL": "< 5%",
                "L": "20%",
                "M": "40%",
                "S": "35%",
                "XS": "< 5%"
            };

            const sizeOrder = ["XL", "L", "M", "S", "XS"];
            sizeOrder.forEach(size => {
                const sizeData = dist[size];
                const ptsLabel = size === "XL" ? "13" : size === "L" ? "8" : size === "M" ? "5" : size === "S" ? "3" : "1";

                // Color code based on percentage
                let rowStyle = "";
                if (size === "XL" && sizeData.percentage > 10) {
                    rowStyle = "background: #fff5f5;";
                } else if (size === "L" && sizeData.percentage > 50) {
                    rowStyle = "background: #fff8e6;";
                }

                html += `
                    <tr style="${rowStyle}">
                        <td><strong>${size}</strong></td>
                        <td>${ptsLabel}</td>
                        <td>${sizeData.count}</td>
                        <td>
                            <div style="display: flex; align-items: center;">
                                <div style="width: 100px; background: #e0e0e0; height: 20px; border-radius: 10px; margin-right: 10px;">
                                    <div style="width: ${sizeData.percentage}%; background: #667eea; height: 100%; border-radius: 10px;"></div>
                                </div>
                                <strong>${sizeData.percentage}%</strong>
                            </div>
                        </td>
                        <td>${sizeData.total_points}</td>
                        <td><span style="color: #666; font-size: 12px;">${idealTargets[size]}</span></td>
                    </tr>
                `;
            });

            html += `
                    </tbody>
                </table>

                <div style="background: #f0f7ff; padding: 20px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #0052cc;">
                    <h4 style="margin: 0 0 10px 0; color: #0052cc;">Sizing Best Practices</h4>
                    <ul style="margin: 0; padding-left: 20px; line-height: 1.8; color: #555;">
                        <li><strong>S (3 pts)</strong> features deliver fastest - aim for 35% of backlog</li>
                        <li><strong>M (5 pts)</strong> features balance speed and value - target 40%</li>
                        <li><strong>L (8 pts)</strong> features acceptable but consider splitting - max 20%</li>
                        <li><strong>XL (13 pts)</strong> features should be rare - keep under 5%</li>
                    </ul>
                </div>
            `;

            container.innerHTML = html;
        }

        // Render Recommendations section
        function renderRecommendations() {
            const container = document.getElementById('analysis-recommendations');
            const sizing = backlogAnalysis.sizing_analysis;

            let html = `
                <h2 style="margin: 0 0 20px 0; color: #333;">Optimization Recommendations</h2>
            `;

            // Show sizing recommendations
            sizing.recommendations.forEach(rec => {
                const className = rec.impact === 'high' ? 'recommendation-high' :
                                rec.impact === 'medium' ? 'recommendation-medium' :
                                rec.impact === 'low' ? 'recommendation-low' : 'recommendation-success';

                const icon = rec.type === 'warning' ? '⚠️' :
                            rec.type === 'info' ? 'ℹ️' : '✅';

                html += `
                    <div class="recommendation-box ${className}">
                        <div style="font-weight: 600; margin-bottom: 8px;">
                            ${icon} ${rec.message}
                        </div>
                    </div>
                `;
            });

            // Show oversized features that should be split
            if (sizing.oversized && sizing.oversized.length > 0) {
                html += `
                    <h3 style="margin: 30px 0 15px 0; color: #333;">Features Recommended for Splitting</h3>
                    <p style="color: #666; margin-bottom: 15px;">
                        The following ${sizing.oversized.length} features are large (XL) and contain multiple concerns.
                        Consider splitting them into smaller, more focused features for faster delivery.
                    </p>
                `;

                sizing.oversized.forEach(item => {
                    const f = item.feature;
                    html += `
                        <div style="padding: 15px; margin: 10px 0; background: #fff8e6; border-left: 4px solid #ff8b00; border-radius: 4px;">
                            <div style="font-weight: 600; color: #333; margin-bottom: 5px;">
                                ${f.key} <span style="background: #ff8b00; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 5px;">${f.points} pts</span>
                            </div>
                            <div style="font-size: 13px; color: #666; margin-bottom: 8px;">${f.summary}</div>
                            <div style="font-size: 12px; color: #555; margin-bottom: 10px;">
                                <strong>Why split:</strong> ${item.reason}
                                <br>
                                <strong>Recommended split:</strong> ${item.suggested_split}
                            </div>
                    `;

                    // Show detailed split recommendations
                    if (item.split_details && item.split_details.length > 0) {
                        html += `
                            <div style="background: white; padding: 12px; border-radius: 4px; margin-top: 10px;">
                                <div style="font-weight: 600; font-size: 11px; color: #ff8b00; margin-bottom: 8px; text-transform: uppercase;">Suggested Feature Breakdown:</div>
                        `;

                        item.split_details.forEach((part, idx) => {
                            html += `
                                <div style="padding: 8px; margin: 5px 0; background: #f9f9f9; border-left: 3px solid #667eea; font-size: 12px;">
                                    <div style="font-weight: 600; color: #333;">
                                        ${f.key}-P${idx + 1}: ${part.name}
                                        <span style="background: #667eea; color: white; padding: 1px 6px; border-radius: 2px; font-size: 10px; margin-left: 5px;">${part.points} pts</span>
                                        <span style="background: #f0f7ff; color: #0052cc; padding: 1px 6px; border-radius: 2px; font-size: 10px; margin-left: 5px;">${part.phase}</span>
                                    </div>
                                </div>
                            `;
                        });

                        html += `
                            </div>
                        `;
                    }

                    html += `
                        </div>
                    `;
                });
            }

            html += `
                <div style="background: #f0fff4; padding: 20px; border-radius: 8px; margin: 30px 0; border-left: 4px solid #28a745;">
                    <h4 style="margin: 0 0 10px 0; color: #28a745;">Expected Benefits</h4>
                    <ul style="margin: 0; padding-left: 20px; line-height: 1.8; color: #555;">
                        <li>Faster feature delivery (smaller features complete quicker)</li>
                        <li>Better capacity planning (more predictable velocity)</li>
                        <li>Improved DP/TP/GA phasing opportunities</li>
                        <li>Reduced risk (smaller changes are less risky)</li>
                        <li>More frequent customer feedback</li>
                    </ul>
                </div>

                <div style="background: #e7f3ff; padding: 20px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #0052cc;">
                    <h4 style="margin: 0 0 10px 0; color: #0052cc;">See Splits in Action</h4>
                    <p style="margin: 0; color: #555; line-height: 1.6;">
                        XL feature splits are automatically applied in the <strong>Draft Plans</strong> tab.
                        Switch to <strong>Optimized (XL Split)</strong> mode to see how split features distribute across release events.
                        Use the <strong>capacity slider</strong> to explore different risk tolerance levels (30-140 pts).
                    </p>
                </div>
            `;

            container.innerHTML = html;
        }

        function closeModal(modalId) {
            document.getElementById(modalId).style.display = 'none';
        }

        // Close modal when clicking outside
        window.onclick = function(event) {
            if (event.target.className === 'modal') {
                event.target.style.display = 'none';
            }
        }
    </script>

    <!-- Help Modal -->
    <div id="help-modal" class="modal">
        <div class="modal-content">
            <span class="modal-close" onclick="closeModal('help-modal')">&times;</span>
            <h2>RHOAI Release Manager - Help Guide</h2>

            <div class="info-card">
                <h4>📊 Track Current Release Cycles</h4>
                <p>Monitor progress on scheduled releases (3.4, 3.5, etc.)</p>
                <ul>
                    <li>Select a release cycle from the dropdown</li>
                    <li>View all 3 events: EA1, EA2, GA</li>
                    <li>See metrics: feature count, story points, capacity status</li>
                    <li>Review feature lists with status and priority</li>
                </ul>
            </div>

            <div class="info-card">
                <h4>📝 Draft Release Plans</h4>
                <p>View AI-recommended 2-year release plan (3.5-3.12)</p>
                <ul>
                    <li><strong>Plan Modes:</strong> Toggle between Baseline and Optimized (XL Split)</li>
                    <li><strong>Capacity Slider:</strong> Adjust per-event limit from 30 to 140 pts</li>
                    <li><strong>Sizing Badges:</strong> See how each feature was sized (JIRA / AI / Keyword)</li>
                    <li><strong>Split Indicators:</strong> Optimized mode shows Part 1/2 for XL splits</li>
                    <li><strong>Release Goals:</strong> Each release shows strategic objectives</li>
                </ul>
            </div>

            <div class="info-card">
                <h4>🔬 Feature Analysis</h4>
                <p>Comprehensive backlog analysis and optimization</p>
                <ul>
                    <li><strong>Sizing Distribution:</strong> Breakdown of feature sizes with recommendations</li>
                    <li><strong>Recommendations:</strong> Specific suggestions for splitting oversized features</li>
                    <li><strong>Efficiency Score:</strong> Measure of delivery efficiency (target: 80+)</li>
                </ul>
            </div>

            <div class="info-card">
                <h4>🔄 Release Cycle Structure</h4>
                <p>Each RHOAI release has 3 events delivered quarterly:</p>
                <ul>
                    <li><strong>EA1:</strong> Early Access 1 (DP/TP features only)</li>
                    <li><strong>EA2:</strong> Early Access 2 (DP/TP features only)</li>
                    <li><strong>GA:</strong> General Availability (DP/TP/GA features)</li>
                </ul>
                <p><strong>Maturity Levels:</strong> DP (Dev Preview) → TP (Tech Preview) → GA (General Availability)</p>
            </div>

            <div class="info-card">
                <h4>📏 Capacity Guidelines</h4>
                <ul>
                    <li>🟢 Conservative: ≤30 pts</li>
                    <li>🟡 Typical: 31-50 pts</li>
                    <li>🟠 Aggressive: 51-80 pts</li>
                    <li>🔴 Maximum: 81-140 pts (historical max within 90% CI)</li>
                </ul>
                <p><strong>Historical baseline:</strong> 27.5 pts/event median, 140 pts historical max release</p>
            </div>

            <div class="info-card">
                <h4>🏷️ Feature Status</h4>
                <ul>
                    <li><strong>Fix Version:</strong> Committed and approved</li>
                    <li><strong>Target Version:</strong> Intended but not committed</li>
                    <li><strong>In Plan:</strong> Ranked in JIRA Advanced Roadmaps Plan</li>
                    <li><strong>Not in Plan:</strong> In RHAISTRAT project but not in plan</li>
                </ul>
            </div>

            <div class="info-card">
                <h4>💡 Tips</h4>
                <ul>
                    <li>Click ℹ️ icons throughout the interface for contextual help</li>
                    <li>Features show story points - auto-sized if not set in JIRA</li>
                    <li>Drag features between events to rebalance capacity</li>
                    <li>Watch capacity meters - stay under 80 pts per event</li>
                </ul>
            </div>
        </div>
    </div>

    <!-- Info Modal -->
    <div id="info-modal" class="modal">
        <div class="modal-content">
            <span class="modal-close" onclick="closeModal('info-modal')">&times;</span>
            <div id="info-modal-content"></div>
        </div>
    </div>
</body>
</html>
"""

    return html


def main():
    """Main execution"""
    print("=" * 70)
    print("RHOAI Release Manager")
    print("=" * 70)
    print()

    # Get JIRA Plan ranking
    plan_id = get_jira_plan_id()
    ranking = get_plan_feature_ranking(plan_id)

    if not ranking:
        print("⚠️  Warning: Could not retrieve plan ranking from JIRA")
        print("   Features will be ordered by default JIRA ranking")
        print()

    # Get all features
    issues = get_all_features()
    features = parse_features(issues, ranking)

    # Group by release
    releases, unscheduled = group_features_by_release(features)

    # Count features in/not in plan
    total_in_plan = sum(1 for f in features if f['in_plan'])
    total_not_in_plan = len(features) - total_in_plan
    unscheduled_in_plan = sum(1 for f in unscheduled if f['in_plan'])
    unscheduled_not_in_plan = len(unscheduled) - unscheduled_in_plan

    print()
    print(f"📊 Summary:")
    print(f"   Total features: {len(features)}")
    print(f"     In JIRA Plan: {total_in_plan}")
    print(f"     Not in JIRA Plan: {total_not_in_plan}")
    print(f"   Unscheduled: {len(unscheduled)}")
    print(f"     In plan: {unscheduled_in_plan}")
    print(f"     Not in plan: {unscheduled_not_in_plan}")
    print(f"   Scheduled releases: {len(releases)}")
    for rel_num in sorted(releases.keys()):
        rel_data = releases[rel_num]
        total = sum(len(rel_data[e]) for e in rel_data)
        print(f"     RHOAI-{rel_num}: {total} features")

    # Show auto-sizing summary
    auto_sized = [f for f in features if f.get('auto_sized', False)]
    if auto_sized:
        print()
        print(f"📏 Auto-Sizing Summary:")
        print(f"   Total features: {len(features)}")
        print(f"   With story points: {len(features) - len(auto_sized)}")
        print(f"   Auto-sized (0 → estimated): {len(auto_sized)}")

        # Count by size
        size_counts = {}
        for f in auto_sized:
            pts = f['points']
            size_counts[pts] = size_counts.get(pts, 0) + 1

        print(f"   Auto-sizing distribution:")
        for pts in sorted(size_counts.keys(), reverse=True):
            size_name = {13: "XL", 8: "L", 5: "M", 3: "S", 1: "XS"}.get(pts, "?")
            print(f"     {pts} pts ({size_name}): {size_counts[pts]} features")

    # Generate auto-schedule recommendation
    print()
    print("🤖 Generating recommended release plan for next 2 years...")
    recommended_plan, schedule = auto_schedule_features(
        unscheduled,
        CAPACITY,
        start_version="3.5",
        num_releases=8  # 2 years at quarterly cadence
    )

    if recommended_plan:
        print(format_plan_summary(recommended_plan, schedule))

        # Count scheduled vs unscheduled
        scheduled_features = set()
        total_scheduled_points = 0
        for bucket_key, bucket_data in recommended_plan.items():
            for feat in bucket_data['features']:
                scheduled_features.add(feat['key'])
                total_scheduled_points += feat['points']

        unscheduled_remaining = len(unscheduled) - len(scheduled_features)
        unscheduled_remaining_points = sum(f['points'] for f in unscheduled if f['key'] not in scheduled_features)

        print()
        print("=" * 60)
        print("📊 Capacity Planning Summary:")
        print(f"   Total unscheduled features: {len(unscheduled)}")
        print(f"   Scheduled in plan (3.5-3.12): {len(scheduled_features)} features, {total_scheduled_points} pts")
        print(f"   Remaining unscheduled: {unscheduled_remaining} features, {unscheduled_remaining_points} pts")
        print(f"   (Features with target dates prioritized)")
        print("=" * 60)

    # Perform backlog analysis
    print()
    print("🔬 Analyzing feature backlog...")
    backlog_analysis = analyze_backlog(features)

    print(f"   Features analyzed: {len(features)}")
    print(f"   Phaseable features (DP/TP/GA): {backlog_analysis['insights']['phasing']['phaseable']} ({backlog_analysis['insights']['phasing']['percentage']}%)")
    print(f"   Delivery efficiency score: {backlog_analysis['insights']['efficiency_score']}/100")

    # Generate optimized plan (using enhanced scheduler with XL splitting)
    print()
    print("🤖 Generating optimized release plan...")
    optimized_plan_result = auto_schedule_features_enhanced(
        unscheduled, CAPACITY, start_version="3.5", num_releases=8, enable_splitting=True
    )

    if optimized_plan_result['splits_applied'] > 0:
        print(f"   Split {optimized_plan_result['splits_applied']} large features into smaller deliverables")

    # Generate HTML
    print()
    print("🎨 Generating HTML interface...")
    html_content = generate_html(
        features,
        releases,
        unscheduled,
        CAPACITY,
        recommended_plan=recommended_plan,
        backlog_analysis=backlog_analysis,
        optimized_plan=optimized_plan_result
    )

    output_file = "release-manager.html"
    with open(output_file, 'w') as f:
        f.write(html_content)

    print(f"✅ Created: {output_file}")
    print()
    print("=" * 70)
    print("Next steps:")
    print("  1. Open release-manager.html in your browser")
    print("  2. Use 'Draft Release Plans' tab to view AI-recommended roadmaps")
    print("  3. Use 'Track Current Releases' tab to monitor progress")
    print("=" * 70)


if __name__ == "__main__":
    main()
