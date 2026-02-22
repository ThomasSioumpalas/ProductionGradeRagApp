import pandas as pd
import uuid
from pathlib import Path


class FinancialKPIs:
    def __init__(self, raw_data: dict):
        """
        raw_data should be a dictionary extracted by your AI agent.
        Example: {'revenue': 1000000, 'net_income': 150000, ...}
        """
        self.data = raw_data

    def calculate_profitability(self):
        """Calculates Profitability KPIs (Δείκτες Αποδοτικότητας)"""
        revenue = self.data.get("revenue", 0)
        net_income = self.data.get("net_income", 0)
        ebt = self.data.get("profit_before_tax", 0)
        equity = self.data.get("total_equity", 1)  # prevent division by zero
        assets = self.data.get("total_assets", 1)

        return {
            "ROE (Return on Equity)": (ebt / equity) * 100,
            "ROA (Return on Assets)": (ebt / assets) * 100,
            "Net Profit Margin (ROS)": (net_income / revenue) * 100 if revenue else 0,
            "Gross Profit Margin": (
                (self.data.get("gross_profit", 0) / revenue) * 100 if revenue else 0
            ),
        }

    def calculate_valuation(self):
        """Calculates Valuation KPIs"""
        market_price = self.data.get("market_price", 0)
        shares = self.data.get("total_shares", 1)
        net_income = self.data.get("net_income", 0)

        eps = net_income / shares
        pe_ratio = market_price / eps if eps != 0 else 0

        return {
            "EPS (Earnings Per Share)": eps,
            "P/E Ratio": pe_ratio,
            "Book Value per Share": self.data.get("total_equity", 0) / shares,
        }

    def calculate_liquidity(self):
        """Calculates Liquidity KPIs"""
        current_assets = self.data.get("current_assets", 0)
        current_liabilities = self.data.get("current_liabilities", 1)

        return {
            "Current Ratio (Γενική Ρευστότητα)": current_assets / current_liabilities
        }

    def get_all_kpis(self):
        """Returns a consolidated dictionary of all metrics"""
        report = {}
        report.update(self.calculate_profitability())
        report.update(self.calculate_valuation())
        report.update(self.calculate_liquidity())
        return report

    def to_dataframe(self):
        """Converts KPIs to a Pandas DataFrame for easy export"""
        kpis = self.get_all_kpis()
        return pd.DataFrame(list(kpis.items()), columns=["Metric", "Value"])


def save_to_excel(df: pd.DataFrame, filename: str = "Financial_Analysis_Report.xlsx") -> str:
    """
    Saves the dataframe inside /app/reports (Docker-safe path)
    and returns the absolute file path.
    """
    reports_dir = Path("/app/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    safe_name = f"{uuid.uuid4().hex}_{filename}"
    file_path = reports_dir / safe_name

    df.to_excel(file_path, index=False)
    return str(file_path)