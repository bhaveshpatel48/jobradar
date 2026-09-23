"""Engineering-role classification.

Title is the primary signal: real boards file engineers under "Security" or
"Applied AI" alongside analysts and sales architects, so department alone
misclassifies. Department only decides titles that don't say either way
(e.g. "Member of Technical Staff").
"""

import re

_ENG_TITLE = re.compile(
    r"\b("
    r"engineer(ing)?|developer|dev ?ops|sre|site reliability|programmer|"
    r"(software|systems?|data|cloud|infrastructure|security|platform|ml) architect|"
    r"firmware|embedded|sdet|qa|quality assurance|test automation|"
    r"machine learning|ml|ai researcher|research scientist|research engineer|"
    r"data scientist|scientist|technical staff|tech lead|technical lead|cto|"
    r"full[- ]?stack|front[- ]?end|back[- ]?end"
    r")\b",
    re.I,
)

# Engineering-sounding titles that sit outside the engineering org.
_NON_ENG_TITLE = re.compile(
    r"\b("
    r"sales|account|solutions?|pre[- ]?sales|customer|support|success|"
    r"recruit(er|ing)?|talent|sourcer|partner(ship)?s?|marketing|growth marketing|"
    r"counsel|legal|paralegal|finance|accountant|payroll|"
    r"program manager|product manager|project manager|"
    r"designer|user experience|ux|writer|content|communications|policy|operations manager|"
    r"executive assistant|office|workplace"
    r")\b",
    re.I,
)

# Deliberately narrow: generic words like "AI", "Security" or "Compute" in a
# department name cover GRC, supply-chain and pre-sales roles too.
_ENG_DEPT = re.compile(r"\b(engineering|software|r ?& ?d|research and development)\b", re.I)
_NON_ENG_DEPT = re.compile(
    r"\b(sales|marketing|finance|legal|people|hr|recruit|operations|support|"
    r"customer|communications|policy|g&a|admin|program management|tax|accounting)\b",
    re.I,
)
# Words that name the person as an engineer (not merely the org, as "Engineering"
# does in "Incident Response Manager - Product & Engineering").
_ENGINEER_NOUN = re.compile(r"\b(engineer|developer|scientist|sre|programmer|architect)\b", re.I)

# Non-engineering role nouns: an engineering-ish department alone must not pull
# these in (e.g. "Protective Intelligence Analyst" under "Security").
_NON_ENG_ROLE = re.compile(
    r"\b(analyst|specialist|coordinator|associate|investigator|officer|"
    r"administrator|trainer|advisor|manager|lead|fellows?|intern|strategist|"
    r"principal|head|operations|compliance|risk|assurance|supply)\b",
    re.I,
)


# Engineering disciplines outside software/tech (data-center, facilities, etc.).
_NON_SOFTWARE_ENG = re.compile(
    r"\b(electrical|mechanical|civil|chemical|structural|hvac|facilities|construction|"
    r"manufacturing|process|controls|commissioning|field service|environmental|geotechnical)\b",
    re.I,
)
_SOFTWARE_WORD = re.compile(
    r"\b(software|developer|full[- ]?stack|back[- ]?end|front[- ]?end|data(?! cent(?:er|re))|ml|machine learning|"
    r"platform|cloud|dev ?ops|security|automation engineer)\b",
    re.I,
)


def is_engineering(title: str | None, department: str | None = None) -> bool:
    title = title or ""
    department = department or ""

    if _NON_SOFTWARE_ENG.search(title) and not _SOFTWARE_WORD.search(title):
        return False

    # A non-engineering org (Finance, TPM, ...) wins unless the title itself
    # calls the person an engineer.
    if department and _NON_ENG_DEPT.search(department) and not _ENGINEER_NOUN.search(title):
        return False

    if _NON_ENG_TITLE.search(title):
        # "Designer" is non-eng, but "Design Engineer" is eng -- an explicit
        # engineer/developer word in the title still wins.
        if not re.search(r"\b(engineer|developer)\b", title, re.I) or re.search(
            r"\b(sales|solutions?|support|customer|success)\b", title, re.I
        ):
            return False

    if _ENG_TITLE.search(title):
        return True

    return bool(
        department
        and _ENG_DEPT.search(department)
        and not _NON_ENG_DEPT.search(department)
        and not _NON_ENG_ROLE.search(title)
    )
