"""Placeholder taxonomy for the public repo.

The real taxonomy.py — the refined sector → subsector → sub-subsector tree,
built over many iterations — is proprietary and kept out of this repo (see
README's "Scope of this repo"). This file exists so the pipeline is
importable and runnable end-to-end for anyone cloning the repo: it keeps the
same top-level sectors (already referenced by name in extractor.py's
classification prompt) and the same function signatures every other module
expects, but with a small illustrative subsector tree and simplified
demote/validate rules instead of the real ones.

Swap this file out for your own taxonomy to classify into different
categories — nothing else in the pipeline needs to change.
"""

TAXONOMY = {
    "AI & Machine Learning": {
        "Applied AI Tools": [],
        "AI Infrastructure": ["Model Hosting", "Data Pipelines"],
        "Uncategorized": [],
    },
    "Cybersecurity": {
        "Threat Detection": [],
        "IAM / PAM": [],
        "Uncategorized": [],
    },
    "Enterprise Software": {
        "Productivity Tools": [
            "AI Email & Communication Assistants",
            "AI Workflow Automation & Digital Workers",
            "AI Meeting & Notes Assistants",
        ],
        "CRM & Sales": [],
        "Business Operations": [],
        "Uncategorized": [],
    },
    "FinTech": {
        "Payments": [],
        "Lending": [],
        "Uncategorized": [],
    },
    "HealthTech": {
        "Clinical Software": [],
        "Patient Engagement": [],
        "Uncategorized": [],
    },
    "Life Sciences": {
        "Drug Discovery": [],
        "Lab Tools": [],
        "Uncategorized": [],
    },
    "Developer Tools & Infrastructure": {
        "AI Driven Developer Productivity": ["AI Code Review"],
        "Cloud Infrastructure": ["Compute & Hosting", "Observability"],
        "Uncategorized": [],
    },
    "Robotics": {
        "Industrial Robotics": [],
        "Consumer Robotics": [],
        "Uncategorized": [],
    },
    "CleanTech": {
        "Renewable Energy": [],
        "Sustainability Software": [],
        "Uncategorized": [],
    },
    "EdTech": {
        "Learning Platforms": [],
        "Assessment Tools": [],
        "Uncategorized": [],
    },
    "E-commerce & Retail": {
        "Storefront Software": [],
        "Supply Chain": [],
        "Uncategorized": [],
    },
    "Marketing Tech": {
        "Ad Tech": [],
        "SEO & Content": [],
        "Uncategorized": [],
    },
    "Quantum Computing": {
        "Quantum Hardware": [],
        "Quantum Software": [],
        "Uncategorized": [],
    },
    "Aerospace & Defense": {
        "Defense Systems": [],
        "Aerospace Manufacturing": [],
        "Uncategorized": [],
    },
    "Energy": {
        "Grid Software": [],
        "Energy Storage": [],
        "Uncategorized": [],
    },
    "Mobility": {
        "Fleet Management": [],
        "Micro-Mobility": [],
        "Uncategorized": [],
    },
    "SpaceTech": {
        "Satellite Systems": [],
        "Launch & Propulsion": [],
        "Uncategorized": [],
    },
    "HRTech": {
        "Recruiting Software": [],
        "Payroll & Benefits": [],
        "Uncategorized": [],
    },
    "InsurTech": {
        "Underwriting Software": [],
        "Claims Automation": [],
        "Uncategorized": [],
    },
    "LegalTech": {
        "Contract Management": [],
        "Compliance Automation": [],
        "Uncategorized": [],
    },
    "PropTech": {
        "Property Management": [],
        "Real Estate Marketplaces": [],
        "Uncategorized": [],
    },
    "Hardware": {
        "Consumer Electronics": [],
        "Industrial Hardware": [],
        "Uncategorized": [],
    },
    "FoodTech": {
        "Alternative Proteins": [],
        "Food Supply Chain": [],
        "Uncategorized": [],
    },
    "AgriTech": {
        "Precision Farming": [],
        "Agri Supply Chain": [],
        "Uncategorized": [],
    },
    "MediaTech": {
        "Content Creation Tools": [],
        "Streaming Infrastructure": [],
        "Uncategorized": [],
    },
    "Consumer Tech": {
        "Consumer Apps": [],
        "Wearables": [],
        "Uncategorized": [],
    },
    "PetTech": {
        "Pet Health": [],
        "Pet Services": [],
        "Uncategorized": [],
    },
}

# Subsectors that are legitimate on their own even without a more specific
# sibling subsector backing them up (extractor.py's _sector_exempt_from_removal).
HORIZONTAL_SUBSECTORS = {
    "Applied AI Tools",
    "Productivity Tools",
}

# Shown to the LLM alongside each subsector's name during Step 2b classification.
# Definitions are optional — any subsector without one here is shown by name only.
SUBSECTOR_DEFINITIONS = {
    "Applied AI Tools": "AI-powered tools applied to a specific business function or workflow.",
    "AI Infrastructure": "Infrastructure for building, hosting, or serving AI/ML models.",
    "Productivity Tools": "General-purpose tools for individual or team productivity.",
    "Business Operations": "Software for running day-to-day back-office operations.",
    "Cloud Infrastructure": "Cloud compute, storage, or infrastructure services.",
    "AI Driven Developer Productivity": "AI-assisted tools that help developers write, review, or ship code faster.",
}


def validate_subsectors(subsectors: list[str], sectors: list[str] = []) -> list[str]:
    """Illustrative placeholder for the real vertical-context rule: drops a
    horizontal subsector once a more specific (non-horizontal) one is also
    present, since the horizontal tag adds no signal at that point.
    """
    non_horizontal = [s for s in subsectors if s not in HORIZONTAL_SUBSECTORS]
    return non_horizontal if non_horizontal else subsectors


def _demote_if_redundant(subsectors: list[str], generic_tag: str) -> list[str]:
    if generic_tag in subsectors and len(subsectors) > 1:
        return [s for s in subsectors if s != generic_tag]
    return subsectors


def demote_generic_erp_tag(subsectors: list[str]) -> list[str]:
    """Illustrative placeholder: drops an overly generic operations tag once a
    more specific subsector is also present."""
    return _demote_if_redundant(subsectors, "Business Operations")


def demote_generic_mlops_tag(subsectors: list[str]) -> list[str]:
    """Illustrative placeholder, same pattern as demote_generic_erp_tag."""
    return _demote_if_redundant(subsectors, "AI Infrastructure")


def demote_generic_compute_tag(subsectors: list[str]) -> list[str]:
    """Illustrative placeholder, same pattern as demote_generic_erp_tag."""
    return _demote_if_redundant(subsectors, "Cloud Infrastructure")


def remove_redundant_uncategorized(subsectors: list[str]) -> list[str]:
    """Drops the 'Uncategorized' fallback once a real subsector is present."""
    return _demote_if_redundant(subsectors, "Uncategorized")
