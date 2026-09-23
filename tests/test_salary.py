"""Salary parsing from free text. Run: python -m unittest discover tests"""

import tests  # noqa: F401,I001 -- must come first: isolates DB_PATH

import unittest

from salary import parse_salary_text, structured

tests.assert_isolated()

PARSES = {
    "Annual Salary: $320,000—$405,000 USD": (320000, 405000, "USD", "year"),
    "The base pay range is $150K – $200K plus equity": (150000, 200000, "USD", "year"),
    "Salary: €60.000 - €80.000 per year": (60000, 80000, "EUR", "year"),
    "£50k-£70k per annum": (50000, 70000, "GBP", "year"),
    "Pay: $60 - $80/hr": (60, 80, "USD", "hour"),
    "compensation USD 150,000 - 200,000": (150000, 200000, "USD", "year"),
    "Salary range: CA$120,000 to CA$150,000": (120000, 150000, "CAD", "year"),
    "Series B, $200M valuation. Salary $180,000 - $220,000": (180000, 220000, "USD", "year"),
    # Verbatim from live postings.
    "the annual base salary range for this role is $140,000-$175,000 USD.": (140000, 175000, "USD", "year"),
    "estimated base salary range for this role is $255k - $290k per year.": (255000, 290000, "USD", "year"),
    "estimated base salary range for this role is $65.38 – $76.92/hr.": (65.38, 76.92, "USD", "hour"),
    "estimated base salary range for this role is $61.54 – $67.79 hourly.": (61.54, 67.79, "USD", "hour"),
    "Annual Base Salary Range: $102,000 — $139,000 USD": (102000, 139000, "USD", "year"),
    "Annual Salary: £130,000 — £165,000 GBP": (130000, 165000, "GBP", "year"),
    "Annual Salary: $420,000 — $495,000 CAD": (420000, 495000, "CAD", "year"),
    "The OTE salary ranges from €3.750 to €5.250 gross per month": (3750, 5250, "EUR", "month"),
    "Gross monthly salary ranging from € 3.600 to € 5.000": (3600, 5000, "EUR", "month"),
    "Gross annual salary ranging from € 77k-103k": (77000, 103000, "EUR", "year"),
    "bruto € 2.850 - € 2.950 per maand": (2850, 2950, "EUR", "month"),
    "Competitive full-time salary (€28,000-€32,200 gross annually)": (28000, 32200, "EUR", "year"),
    "The Base Rate for this position is $35- $46 per hour": (35, 46, "USD", "hour"),
    "The base salary range is 248,000 USD - 396,750 USD.": (248000, 396750, "USD", "year"),
    "The base salary range is 184,000 USD - 287,500 USD for Level 4, and 224,000 USD - 356,500 USD for Level 5":
        (184000, 287500, "USD", "year"),
    "we offer a base pay of $166,500 - $291,400 , plus equity": (166500, 291400, "USD", "year"),
    "180K - 220K USD a year": (180000, 220000, "USD", "year"),
}

REJECTS = [
    "We raised $5M - $10M from investors",
    "Great 401(k) match and 20 - 30 days PTO",
    "$1.5k - $2k",
    "Deals range from $200k to $500k contract values",
    "A $1,500 work-from-home stipend",
    "Revenue grew from $2M to $8M",
    "184,000 USD - 287,500 USD for Level 4",  # no salary wording, no period
    "Hourly shift patterns vary. Budget: $40,000 - $60,000 for tooling",
    "",
    None,
]


class SalaryTextTest(unittest.TestCase):
    def test_parses_ranges(self):
        for text, expected in PARSES.items():
            with self.subTest(text=text):
                r = parse_salary_text(text)
                self.assertIsNotNone(r)
                got = (r["salary_min"], r["salary_max"], r["salary_currency"], r["salary_period"])
                self.assertEqual(got, expected)
                self.assertEqual(r["salary_source"], "description")

    def test_rejects_non_salary_numbers(self):
        for text in REJECTS:
            with self.subTest(text=text):
                self.assertIsNone(parse_salary_text(text))


class StructuredSalaryTest(unittest.TestCase):
    def test_normalises_fields(self):
        r = structured(150000, "200000", "usd", "YEAR", None)
        self.assertEqual((r["salary_min"], r["salary_max"], r["salary_currency"], r["salary_period"]),
                         (150000, 200000, "USD", "year"))
        self.assertEqual(r["salary_source"], "ats")

    def test_period_from_title_or_magnitude(self):
        self.assertEqual(structured(45.2, 80.3, "USD", "Hourly Pay Range:")["salary_period"], "hour")
        self.assertEqual(structured(165000, 190000, "USD", "Annual Base Salary Range:")["salary_period"], "year")
        self.assertEqual(structured(45.2, 80.3, "USD", None)["salary_period"], "hour")
        self.assertEqual(structured(2850, 2950, "EUR", "month")["salary_period"], "month")

    def test_placeholder_range_is_dropped(self):
        self.assertIsNone(structured(1, 1, "GBP", "Annual Salary"))

    def test_text_only_and_empty(self):
        self.assertEqual(structured(text="$150K – $200K")["salary_text"], "$150K – $200K")
        self.assertIsNone(structured())


if __name__ == "__main__":
    unittest.main()
