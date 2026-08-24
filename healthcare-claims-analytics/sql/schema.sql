-- ============ DIMENSION TABLES ============

CREATE TABLE dim_payer (
    payer_id      TEXT PRIMARY KEY,
    payer_name    TEXT NOT NULL,
    payer_type    TEXT NOT NULL,   -- 'Medicare', 'Medicaid', 'Commercial'
    plan_type     TEXT             -- 'MA', 'PPO', 'HMO', 'EPO'
);

CREATE TABLE dim_provider (
    provider_id     TEXT PRIMARY KEY,
    provider_name   TEXT NOT NULL,
    npi             TEXT NOT NULL,  -- 10-digit National Provider Identifier (simulate format)
    specialty       TEXT,
    department      TEXT
);

CREATE TABLE dim_cpt (
    cpt_code        TEXT PRIMARY KEY,
    cpt_description TEXT NOT NULL,
    category        TEXT            -- 'E/M', 'Surgery', 'Imaging', 'Lab', 'Preventive'
);

CREATE TABLE dim_icd10 (
    icd10_code        TEXT PRIMARY KEY,
    icd10_description TEXT NOT NULL,
    chapter           TEXT          -- official ICD-10 chapter grouping
);

CREATE TABLE dim_denial_reason (
    reason_code        TEXT PRIMARY KEY,  -- use real CARC codes (e.g., 'CO-16', 'CO-197')
    reason_description TEXT NOT NULL,
    category           TEXT NOT NULL      -- 'Eligibility', 'Coding Error', 'Medical Necessity',
                                          -- 'Authorization', 'Timely Filing', 'Other'
);

CREATE TABLE dim_date (
    full_date       DATE PRIMARY KEY,
    day_of_week     TEXT,
    month           INT,
    quarter         INT,
    year            INT,
    is_weekend      BOOLEAN
);

-- ============ FACT TABLE ============

CREATE TABLE fact_claims (
    claim_id            TEXT PRIMARY KEY,
    patient_id          TEXT NOT NULL,     -- synthetic ID, no dimension table needed for v1
    provider_id         TEXT REFERENCES dim_provider(provider_id),
    payer_id            TEXT REFERENCES dim_payer(payer_id),
    cpt_code            TEXT REFERENCES dim_cpt(cpt_code),
    icd10_code          TEXT REFERENCES dim_icd10(icd10_code),
    date_of_service     DATE REFERENCES dim_date(full_date),
    date_submitted      DATE,
    date_adjudicated    DATE,
    billed_amount       NUMERIC(10,2),
    allowed_amount      NUMERIC(10,2),
    paid_amount         NUMERIC(10,2),
    claim_status        TEXT NOT NULL,     -- 'Paid', 'Denied', 'Partially Paid'
    denial_reason_code  TEXT REFERENCES dim_denial_reason(reason_code),
    is_preventable      BOOLEAN
);

-- Helpful indexes for the agent's typical query patterns
CREATE INDEX idx_claims_payer ON fact_claims(payer_id);
CREATE INDEX idx_claims_provider ON fact_claims(provider_id);
CREATE INDEX idx_claims_status ON fact_claims(claim_status);
CREATE INDEX idx_claims_dos ON fact_claims(date_of_service);
