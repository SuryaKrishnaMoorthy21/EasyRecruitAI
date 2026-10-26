"""
EasyRecruit ATS 3.0 — Company Registry Seeding
──────────────────────────────────────────────
Every company that appears in the Job Market (datasets/job_notifications.json)
gets a row here with a dedicated recruiter e-mail domain. A recruiter can only
self-register for a company if their e-mail matches that company's domain,
and even then their account sits in 'pending' status until that company's
admin approves it.

SECURITY — this module seeds the company registry ONLY (names and public
e-mail domains: not secret). It deliberately creates NO user accounts.

Earlier versions pre-created one ``company_admin`` per company with a fixed
password stored in this file. Those passwords were published in source and in
distributed ZIPs, and the seeder recreated any missing admin on every start.
That behaviour has been removed. Privileged accounts are now provisioned
explicitly, with an operator-chosen secret that never lives in source control:

    python -m app.admin_cli provision --role company_admin \\
        --company "<registered company name>" --email <address>

or, for the very first administrator on a fresh deployment, via the opt-in
INITIAL_ADMIN_* environment variables (see app/db/admin_provisioning.py and
DEPLOY.md). Accounts already present in an existing database are never
modified, disabled or deleted by this module.
"""
import logging

logger = logging.getLogger(__name__)

# name                              recruiter_domain
COMPANIES = [
    ("Zoho Corporation",                "zohorecruiter.com"),
    ("Freshworks",                      "freshworksrecruiter.com"),
    ("Tata Consultancy Services",       "tcsrecruiter.com"),
    ("Infosys BPM",                     "infosysbpmrecruiter.com"),
    ("Cognizant Technology Solutions",  "cognizantrecruiter.com"),
    ("Hexaware Technologies",           "hexawarerecruiter.com"),
    ("Sutherland Global Services",      "sutherlandrecruiter.com"),
    ("TVS Motor Company",               "tvsmotorrecruiter.com"),
    ("Rane Group",                      "ranegrouprecruiter.com"),
    ("Wipro Limited",                   "wiprorecruiter.com"),
    ("ELGI Equipments",                 "elgirecruiter.com"),
    ("Amazon Development Centre India", "amazonindiarecruiter.com"),
    ("Chargebee",                       "chargebeerecruiter.com"),
    ("Ramco Systems",                   "ramcorecruiter.com"),
    ("Ford Motor Company India",        "fordindiarecruiter.com"),
    ("Intellect Design Arena",          "intellectrecruiter.com"),
    ("Ashok Leyland",                   "ashokleylandrecruiter.com"),
    ("Standard Chartered GBS",          "standardcharteredrecruiter.com"),
    ("Tata Elxsi",                      "tataelxsirecruiter.com"),
    ("NatWest Group India",             "natwestrecruiter.com"),
]


def seed_companies(db) -> None:
    """
    Idempotent: safe to call on every app startup.

    Inserts any company row that doesn't exist yet and nothing else. It never
    updates or deletes an existing company, and never creates, changes or
    removes a user account.
    """
    for name, domain in COMPANIES:
        try:
            existing_company = db.fetch_one(
                "SELECT id FROM companies WHERE name = ?", (name,)
            )
            if not existing_company:
                db.insert(
                    "INSERT INTO companies (name, recruiter_domain) VALUES (?, ?)",
                    (name, domain),
                )
                logger.info(f"Seeded company: {name} ({domain})")
        except Exception as exc:
            logger.warning(f"Company seed skipped for {name}: {exc}")
