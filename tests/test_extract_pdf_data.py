import unittest
from pathlib import Path

from app import extract_pdf_data


DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class ExtractPdfDataTests(unittest.TestCase):
    def test_extracts_expected_fields_from_regression_samples(self) -> None:
        expectations = {
            "Copy of 研究生启航计划合约_-_Yaqi_Li.pdf": {
                "client_name": "Yaqi Li",
                "plan_name": "研究生启航申请计划",
                "base_fee": "9800",
                "down_payment": "3920",
            },
            "Copy of 美研申请菁英计划26F-Jiayin Kuang.pdf": {
                "client_name": "匡佳音",
                "plan_name": "研究生菁英申请计划",
                "base_fee": "12300",
                "down_payment": "7380",
            },
            "Copy of 美研申请菁英计划（带名企实习）26FMI合约-Jiaqi Chai.pdf": {
                "client_name": "Jiaqi Chai",
                "plan_name": "研究生菁英申请计划",
                "base_fee": "14800",
                "down_payment": "5920",
            },
            "Copy of 请使用 Docusign 签署： 26Fall—SVIP—沛祺 Cao pdf.pdf": {
                "client_name": "沛祺",
                "plan_name": "SVIP冲藤计划",
                "base_fee": "28000",
                "down_payment": "16800",
            },
        }

        for filename, expected in expectations.items():
            with self.subTest(filename=filename):
                result = extract_pdf_data((DATA_DIR / filename).read_bytes(), filename)
                self.assertEqual(result["client_name"], expected["client_name"])
                self.assertEqual(result["plan_name"], expected["plan_name"])
                self.assertEqual(result["base_fee"], expected["base_fee"])
                self.assertEqual(result["down_payment"], expected["down_payment"])


if __name__ == "__main__":
    unittest.main()
