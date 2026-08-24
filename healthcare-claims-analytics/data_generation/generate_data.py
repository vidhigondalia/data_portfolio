"""Generate synthetic healthcare claims data matching sql/schema.sql.

Writes one CSV per table to data_generation/output/, ready to load into
Postgres. Reproducible: all randomness derives from SEED in .env.

    python data_generation/generate_data.py
    python data_generation/generate_data.py --claims 5000 --seed 7

Three patterns are planted deliberately so downstream analysis has something
real to find. They are documented in PLANTED_PATTERNS below and echoed in the
run summary; see that constant before "discovering" anything in the data.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from faker import Faker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path(__file__).resolve().parent / "output"

DEFAULT_CLAIMS = 20_000
DEFAULT_SEED = 42
END_DATE = date(2025, 12, 31)
START_DATE = date(2024, 1, 1)          # two-year date dimension
N_PATIENTS = 4_000
BASE_DENIAL_RATE = 0.11                # industry-typical initial denial rate

PLANTED_PATTERNS = """\
1. Payer scrutiny  : 2 payers deny a specific CPT/ICD subset at ~2.5x base rate
2. Provider drift  : 1 provider's denial rate ramps 1.0x -> 3.0x over 2nd year
3. Reason mix      : denial reason categories are weighted by payer_type
"""


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

PAYERS = [
    # (payer_id, name, payer_type, plan_type)
    ("PAY001", "Meridian Senior Advantage",   "Medicare",   "MA"),
    ("PAY002", "Golden State Medicare Plus",  "Medicare",   "MA"),
    ("PAY003", "Evergreen Medicare Choice",   "Medicare",   "MA"),
    ("PAY004", "Statewide Health Partners",   "Medicaid",   "HMO"),
    ("PAY005", "CommunityFirst Medicaid",     "Medicaid",   "HMO"),
    ("PAY006", "Bluegrass Care Medicaid",     "Medicaid",   "EPO"),
    ("PAY007", "Atlas National Insurance",    "Commercial", "PPO"),
    ("PAY008", "Northwind Health Group",      "Commercial", "PPO"),
    ("PAY009", "Summit Employer Benefits",    "Commercial", "HMO"),
    ("PAY010", "Cascade Select Health",       "Commercial", "EPO"),
]

SPECIALTIES = [
    ("Internal Medicine",  "Primary Care"),
    ("Family Medicine",    "Primary Care"),
    ("Cardiology",         "Cardiovascular"),
    ("Orthopedic Surgery", "Surgical Services"),
    ("General Surgery",    "Surgical Services"),
    ("Radiology",          "Imaging"),
    ("Emergency Medicine", "Emergency"),
    ("Endocrinology",      "Medical Specialties"),
    ("Pulmonology",        "Medical Specialties"),
    ("Psychiatry",         "Behavioral Health"),
    ("Pathology",          "Laboratory"),
    ("Obstetrics",         "Women's Health"),
    ("Gastroenterology",   "Medical Specialties"),
    ("Neurology",          "Medical Specialties"),
    ("Nephrology",         "Medical Specialties"),
]

# (code, description, category, typical_billed_amount)
CPT_CODES = [
    ("99213", "Office visit, established patient, low complexity",   "E/M", 145),
    ("99214", "Office visit, established patient, moderate",         "E/M", 210),
    ("99215", "Office visit, established patient, high complexity",  "E/M", 295),
    ("99203", "Office visit, new patient, low complexity",           "E/M", 195),
    ("99204", "Office visit, new patient, moderate complexity",      "E/M", 290),
    ("99284", "Emergency department visit, moderate severity",       "E/M", 720),

    ("29881", "Arthroscopy, knee, with meniscectomy",             "Surgery", 4850),
    ("27447", "Total knee arthroplasty",                          "Surgery", 28500),
    ("47562", "Laparoscopic cholecystectomy",                     "Surgery", 9200),
    ("44970", "Laparoscopic appendectomy",                        "Surgery", 8100),
    ("64483", "Injection, lumbar transforaminal epidural",        "Surgery", 1950),
    ("66984", "Cataract removal with intraocular lens insertion", "Surgery", 3400),

    ("70450", "CT head/brain, without contrast",              "Imaging", 1150),
    ("72148", "MRI lumbar spine, without contrast",           "Imaging", 2400),
    ("73721", "MRI lower extremity joint, without contrast",  "Imaging", 2250),
    ("71046", "Chest X-ray, 2 views",                         "Imaging", 285),
    ("76700", "Ultrasound, abdominal, complete",              "Imaging", 640),
    ("93306", "Echocardiography, transthoracic, complete",    "Imaging", 1420),

    ("80053", "Comprehensive metabolic panel",     "Lab", 95),
    ("85025", "Complete blood count with differential", "Lab", 62),
    ("80061", "Lipid panel",                       "Lab", 88),
    ("83036", "Hemoglobin A1c",                    "Lab", 74),
    ("84443", "Thyroid stimulating hormone (TSH)", "Lab", 105),
    ("81001", "Urinalysis, automated with microscopy", "Lab", 48),

    ("99395", "Preventive visit, established patient, 18-39 yrs", "Preventive", 265),
    ("99396", "Preventive visit, established patient, 40-64 yrs", "Preventive", 295),
    ("99385", "Preventive visit, new patient, 18-39 yrs",         "Preventive", 320),
    ("G0438", "Annual wellness visit, initial",                   "Preventive", 285),
    ("G0439", "Annual wellness visit, subsequent",                "Preventive", 195),
    ("77067", "Screening mammography, bilateral",                 "Preventive", 380),
]

# (code, description, chapter)
ICD10_CODES = [
    ("E11.9",  "Type 2 diabetes mellitus without complications",       "Endocrine, nutritional and metabolic diseases"),
    ("E11.65", "Type 2 diabetes mellitus with hyperglycemia",          "Endocrine, nutritional and metabolic diseases"),
    ("E78.5",  "Hyperlipidemia, unspecified",                          "Endocrine, nutritional and metabolic diseases"),
    ("E03.9",  "Hypothyroidism, unspecified",                          "Endocrine, nutritional and metabolic diseases"),
    ("E66.9",  "Obesity, unspecified",                                 "Endocrine, nutritional and metabolic diseases"),
    ("E10.9",  "Type 1 diabetes mellitus without complications",       "Endocrine, nutritional and metabolic diseases"),

    ("I10",    "Essential (primary) hypertension",                     "Diseases of the circulatory system"),
    ("I25.10", "Atherosclerotic heart disease of native coronary artery", "Diseases of the circulatory system"),
    ("I48.91", "Unspecified atrial fibrillation",                      "Diseases of the circulatory system"),
    ("I50.9",  "Heart failure, unspecified",                           "Diseases of the circulatory system"),
    ("I63.9",  "Cerebral infarction, unspecified",                     "Diseases of the circulatory system"),
    ("I21.4",  "Non-ST elevation myocardial infarction",               "Diseases of the circulatory system"),

    ("J45.909", "Unspecified asthma, uncomplicated",                   "Diseases of the respiratory system"),
    ("J44.9",  "Chronic obstructive pulmonary disease, unspecified",   "Diseases of the respiratory system"),
    ("J18.9",  "Pneumonia, unspecified organism",                      "Diseases of the respiratory system"),
    ("J02.9",  "Acute pharyngitis, unspecified",                       "Diseases of the respiratory system"),
    ("J30.9",  "Allergic rhinitis, unspecified",                       "Diseases of the respiratory system"),
    ("J06.9",  "Acute upper respiratory infection, unspecified",       "Diseases of the respiratory system"),

    ("M17.11", "Unilateral primary osteoarthritis, right knee",        "Diseases of the musculoskeletal system"),
    ("M54.50", "Low back pain, unspecified",                           "Diseases of the musculoskeletal system"),
    ("M25.511", "Pain in right shoulder",                              "Diseases of the musculoskeletal system"),
    ("M79.7",  "Fibromyalgia",                                         "Diseases of the musculoskeletal system"),
    ("M15.0",  "Primary generalized (osteo)arthritis",                 "Diseases of the musculoskeletal system"),
    ("M81.0",  "Age-related osteoporosis without fracture",            "Diseases of the musculoskeletal system"),

    ("F32.9",  "Major depressive disorder, single episode, unspecified", "Mental and behavioural disorders"),
    ("F41.1",  "Generalized anxiety disorder",                         "Mental and behavioural disorders"),
    ("F33.1",  "Major depressive disorder, recurrent, moderate",       "Mental and behavioural disorders"),
    ("F17.210", "Nicotine dependence, cigarettes, uncomplicated",      "Mental and behavioural disorders"),
    ("F90.9",  "Attention-deficit hyperactivity disorder, unspecified", "Mental and behavioural disorders"),
    ("F43.10", "Post-traumatic stress disorder, unspecified",          "Mental and behavioural disorders"),
]

# Real CARC (Claim Adjustment Reason Codes). 18 codes across 5 categories.
DENIAL_REASONS = [
    ("CO-27",  "Expenses incurred after coverage terminated",                    "Eligibility"),
    ("CO-26",  "Expenses incurred prior to coverage",                            "Eligibility"),
    ("CO-31",  "Patient cannot be identified as our insured",                    "Eligibility"),
    ("CO-22",  "This care may be covered by another payer per coordination of benefits", "Eligibility"),

    ("CO-16",  "Claim/service lacks information or has submission/billing error", "Coding Error"),
    ("CO-4",   "Procedure code is inconsistent with the modifier used or a required modifier is missing", "Coding Error"),
    ("CO-11",  "The diagnosis is inconsistent with the procedure",               "Coding Error"),
    ("CO-181", "Procedure code was invalid on the date of service",              "Coding Error"),
    ("CO-8",   "The procedure code is inconsistent with the provider type/specialty", "Coding Error"),

    ("CO-50",  "Non-covered services because this is not deemed a medical necessity", "Medical Necessity"),
    ("CO-55",  "Procedure/treatment is deemed experimental/investigational",     "Medical Necessity"),
    ("CO-56",  "Procedure/treatment has not been deemed proven to be effective", "Medical Necessity"),
    ("CO-40",  "Charges do not meet qualifications for emergent/urgent care",    "Medical Necessity"),

    ("CO-197", "Precertification/authorization/notification absent",             "Authorization"),
    ("CO-198", "Precertification/authorization exceeded",                        "Authorization"),
    ("CO-15",  "The authorization number is missing, invalid, or does not apply", "Authorization"),
    ("CO-243", "Services not authorized by network/primary care providers",      "Authorization"),

    ("CO-29",  "The time limit for filing has expired",                          "Timely Filing"),
]

# Denials an RCM team could have avoided through front-end process (eligibility
# checks, prior auth, clean coding, filing on time). Medical necessity denials
# turn on clinical/payer policy judgement, so they are not counted preventable.
PREVENTABLE_BY_CATEGORY = {
    "Eligibility":       True,
    "Coding Error":      True,
    "Authorization":     True,
    "Timely Filing":     True,
    "Medical Necessity": False,
}

# Pattern 3: which denial categories each payer type leans on.
REASON_WEIGHTS_BY_PAYER_TYPE = {
    "Medicare":   {"Authorization": 0.34, "Medical Necessity": 0.30, "Coding Error": 0.18,
                   "Eligibility": 0.12, "Timely Filing": 0.06},
    "Medicaid":   {"Eligibility": 0.42, "Coding Error": 0.22, "Authorization": 0.14,
                   "Medical Necessity": 0.12, "Timely Filing": 0.10},
    "Commercial": {"Coding Error": 0.33, "Authorization": 0.28, "Medical Necessity": 0.20,
                   "Eligibility": 0.13, "Timely Filing": 0.06},
}

# Allowed amount as a share of billed, by payer type.
ALLOWED_RATE = {"Medicare": (0.28, 0.42), "Medicaid": (0.22, 0.35), "Commercial": (0.45, 0.65)}

# Pattern 1: these payers scrutinise this CPT/ICD subset.
SCRUTINY_PAYERS = ("PAY002", "PAY007")
SCRUTINY_CPT = {"72148", "73721", "64483", "29881", "27447", "93306"}
SCRUTINY_ICD = {"M54.50", "M17.11", "M25.511", "M79.7", "M15.0"}
SCRUTINY_MULTIPLIER = 2.5

# Pattern 2: this provider's denial rate ramps through the second year.
DRIFT_PROVIDER_INDEX = 3        # index into the generated provider list
DRIFT_MAX_MULTIPLIER = 3.0


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def luhn_check_digit(payload: str) -> str:
    """Check digit for an NPI, per the CMS spec (Luhn over '80840' + 9 digits)."""
    digits = [int(c) for c in ("80840" + payload)]
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 0:          # positions are odd-indexed from the right
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def make_npi(rng: np.random.Generator) -> str:
    """A 10-digit NPI: leading 1, 8 random digits, valid Luhn check digit."""
    body = "1" + "".join(str(d) for d in rng.integers(0, 10, 8))
    return body + luhn_check_digit(body)


def build_payers() -> pd.DataFrame:
    return pd.DataFrame(PAYERS, columns=["payer_id", "payer_name", "payer_type", "plan_type"])


def build_providers(rng: np.random.Generator, fake: Faker) -> pd.DataFrame:
    rows = []
    seen: set[str] = set()
    for i, (specialty, department) in enumerate(SPECIALTIES, start=1):
        npi = make_npi(rng)
        while npi in seen:                      # NPIs are unique in reality
            npi = make_npi(rng)
        seen.add(npi)
        rows.append({
            "provider_id": f"PRV{i:03d}",
            "provider_name": f"Dr. {fake.first_name()} {fake.last_name()}",
            "npi": npi,
            "specialty": specialty,
            "department": department,
        })
    return pd.DataFrame(rows)


def build_cpt() -> pd.DataFrame:
    return pd.DataFrame(
        [(c, d, cat) for c, d, cat, _ in CPT_CODES],
        columns=["cpt_code", "cpt_description", "category"],
    )


def build_icd10() -> pd.DataFrame:
    return pd.DataFrame(ICD10_CODES, columns=["icd10_code", "icd10_description", "chapter"])


def build_denial_reasons() -> pd.DataFrame:
    return pd.DataFrame(DENIAL_REASONS, columns=["reason_code", "reason_description", "category"])


def build_dim_date() -> pd.DataFrame:
    days = pd.date_range(START_DATE, END_DATE, freq="D")
    return pd.DataFrame({
        "full_date": days.date,
        "day_of_week": days.day_name(),
        "month": days.month,
        "quarter": days.quarter,
        "year": days.year,
        "is_weekend": days.dayofweek >= 5,
    })


# ---------------------------------------------------------------------------
# Fact table
# ---------------------------------------------------------------------------

def build_claims(n: int, rng: np.random.Generator, payers, providers, cpt, icd10,
                 reasons) -> pd.DataFrame:
    billed_lookup = {c: amt for c, _, _, amt in CPT_CODES}
    cpt_category = dict(zip(cpt.cpt_code, cpt.category))
    payer_type = dict(zip(payers.payer_id, payers.payer_type))
    reasons_by_cat = {cat: grp.reason_code.tolist() for cat, grp in reasons.groupby("category")}
    preventable = dict(zip(reasons.reason_code, reasons.category.map(PREVENTABLE_BY_CATEGORY)))

    drift_provider = providers.provider_id.iloc[DRIFT_PROVIDER_INDEX]
    total_days = (END_DATE - START_DATE).days
    midpoint = total_days / 2

    # Draw independent columns in bulk; per-row logic follows.
    payer_ids = rng.choice(payers.payer_id.to_numpy(), n)
    provider_ids = rng.choice(providers.provider_id.to_numpy(), n)
    cpt_ids = rng.choice(cpt.cpt_code.to_numpy(), n)
    icd_ids = rng.choice(icd10.icd10_code.to_numpy(), n)
    day_offsets = rng.integers(0, total_days + 1, n)
    patient_ids = rng.integers(1, N_PATIENTS + 1, n)
    submit_lag = rng.integers(1, 16, n)
    adjud_lag = rng.integers(5, 46, n)
    denial_roll = rng.random(n)
    status_roll = rng.random(n)
    partial_frac = rng.uniform(0.30, 0.90, n)

    rows = []
    for i in range(n):
        pid, prov = payer_ids[i], provider_ids[i]
        cpt_code, icd_code = cpt_ids[i], icd_ids[i]
        ptype = payer_type[pid]
        dos = START_DATE + timedelta(days=int(day_offsets[i]))

        # --- denial probability -------------------------------------------
        rate = BASE_DENIAL_RATE

        # Pattern 1: targeted payer scrutiny on a CPT/ICD subset.
        if pid in SCRUTINY_PAYERS and cpt_code in SCRUTINY_CPT and icd_code in SCRUTINY_ICD:
            rate *= SCRUTINY_MULTIPLIER

        # Pattern 2: one provider degrades linearly through the second half.
        if prov == drift_provider and day_offsets[i] > midpoint:
            progress = (day_offsets[i] - midpoint) / midpoint      # 0.0 -> 1.0
            rate *= 1.0 + progress * (DRIFT_MAX_MULTIPLIER - 1.0)

        rate = min(rate, 0.95)
        denied = denial_roll[i] < rate

        # --- amounts -------------------------------------------------------
        base = billed_lookup[cpt_code]
        billed = round(float(base) * float(rng.uniform(0.85, 1.20)), 2)
        lo, hi = ALLOWED_RATE[ptype]
        allowed = round(billed * float(rng.uniform(lo, hi)), 2)

        if denied:
            status = "Denied"
            allowed_out, paid = 0.00, 0.00
            # Pattern 3: reason category weighted by payer type.
            weights = REASON_WEIGHTS_BY_PAYER_TYPE[ptype]
            cats, probs = list(weights.keys()), list(weights.values())
            category = str(rng.choice(cats, p=probs))
            reason = str(rng.choice(reasons_by_cat[category]))
            is_prev = preventable[reason]
        elif status_roll[i] < 0.15:
            status = "Partially Paid"
            allowed_out = allowed
            paid = round(allowed * float(partial_frac[i]), 2)
            reason, is_prev = None, None
        else:
            status = "Paid"
            allowed_out, paid = allowed, allowed
            reason, is_prev = None, None

        rows.append({
            "claim_id": f"CLM{i + 1:08d}",
            "patient_id": f"PAT{patient_ids[i]:06d}",
            "provider_id": prov,
            "payer_id": pid,
            "cpt_code": cpt_code,
            "icd10_code": icd_code,
            "date_of_service": dos,
            "date_submitted": dos + timedelta(days=int(submit_lag[i])),
            "date_adjudicated": dos + timedelta(days=int(submit_lag[i]) + int(adjud_lag[i])),
            "billed_amount": billed,
            "allowed_amount": allowed_out,
            "paid_amount": paid,
            "claim_status": status,
            "denial_reason_code": reason,
            "is_preventable": is_prev,
        })

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def summarise(claims: pd.DataFrame, payers: pd.DataFrame, providers: pd.DataFrame) -> None:
    denied = claims.claim_status == "Denied"
    print(f"\nOverall denial rate: {denied.mean():.1%}")

    print("\nPattern 1 - targeted CPT/ICD subset denial rate:")
    subset = claims.cpt_code.isin(SCRUTINY_CPT) & claims.icd10_code.isin(SCRUTINY_ICD)
    tgt = claims[subset & claims.payer_id.isin(SCRUTINY_PAYERS)]
    oth = claims[subset & ~claims.payer_id.isin(SCRUTINY_PAYERS)]
    t = (tgt.claim_status == "Denied").mean()
    o = (oth.claim_status == "Denied").mean()
    print(f"  {'/'.join(SCRUTINY_PAYERS)}: {t:.1%} (n={len(tgt)})   other payers: {o:.1%} (n={len(oth)})")
    print(f"  lift: {t / o:.1f}x" if o else "  lift: n/a")

    print("\nPattern 2 - drift provider by half:")
    drift = providers.provider_id.iloc[DRIFT_PROVIDER_INDEX]
    mid = START_DATE + timedelta(days=(END_DATE - START_DATE).days // 2)
    d = claims[claims.provider_id == drift]
    for label, sel in (("1st half", d.date_of_service <= mid), ("2nd half", d.date_of_service > mid)):
        sub = d[sel]
        print(f"  {drift} {label}: {(sub.claim_status == 'Denied').mean():.1%} (n={len(sub)})")

    print("\nPattern 3 - denial reason category share by payer type:")
    dn = claims[denied].merge(payers[["payer_id", "payer_type"]], on="payer_id")
    cat = dict(DENIAL_REASONS and [(c, k) for c, _, k in DENIAL_REASONS])
    dn["category"] = dn.denial_reason_code.map(cat)
    share = pd.crosstab(dn.payer_type, dn.category, normalize="index")
    print(share.mul(100).round(1).to_string())

    print(f"\nPreventable share of denials: {claims.loc[denied, 'is_preventable'].mean():.1%}")


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--claims", type=int, default=int(os.getenv("NUM_CLAIMS", DEFAULT_CLAIMS)))
    parser.add_argument("--seed", type=int, default=int(os.getenv("SEED", DEFAULT_SEED)))
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args()

    if args.claims < 1:
        sys.exit("--claims must be positive")

    rng = np.random.default_rng(args.seed)
    fake = Faker()
    Faker.seed(args.seed)

    print(f"seed={args.seed}  claims={args.claims:,}  range={START_DATE}..{END_DATE}")

    payers = build_payers()
    providers = build_providers(rng, fake)
    cpt = build_cpt()
    icd10 = build_icd10()
    reasons = build_denial_reasons()
    dim_date = build_dim_date()
    claims = build_claims(args.claims, rng, payers, providers, cpt, icd10, reasons)

    args.output.mkdir(parents=True, exist_ok=True)
    tables = {
        "dim_payer": payers, "dim_provider": providers, "dim_cpt": cpt,
        "dim_icd10": icd10, "dim_denial_reason": reasons, "dim_date": dim_date,
        "fact_claims": claims,
    }
    print()
    for name, df in tables.items():
        path = args.output / f"{name}.csv"
        df.to_csv(path, index=False)
        print(f"  {path.relative_to(PROJECT_ROOT)}  {len(df):,} rows")

    print(f"\nPlanted patterns:\n{PLANTED_PATTERNS}")
    summarise(claims, payers, providers)


if __name__ == "__main__":
    main()
