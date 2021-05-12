from datetime import datetime
import shutil
import tempfile
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as ani
import yfinance as yf
from forex_python.converter import CurrencyRates
from jinja2 import Environment, PackageLoader, select_autoescape
from .balance import Balance
from .price import Currency
from .common import log_info, log_warning
from .holding import Holding
from .analysis import Analysis
from .config import Config


class Project:
    """
    Root class for handling a project and the creation of a report
    """

    def __init__(self, config: str):

        self.cfg = Config(path=config)

        # Conversion rates to the dest currency
        self._dst_currency_rates = CurrencyRates().get_rates(self.cfg.currency.name)

        # Balances after each transaction (max one balance per day)
        self.balances = Balance.get_balances(transactions=self.cfg.transactions)

        # For each holding identity (key) this list contains the
        # first one ever hold
        self._first_holdings = self._get_first_holdings()

        # Maps holdings to their currency
        self._holding_to_currency = self._get_currencies()

        # Daily prices for every holding
        self._prices = self._get_prices()

        # All meta attributes
        self._meta = self.cfg.get_meta_attributes(
            [h["holding"] for h in self._first_holdings.values()]
        )

    def _get_attrs_report_data(self, analysis: Analysis, allocations: pd.DataFrame):
        reports = {}
        for attr in self._meta:
            log_info(f"Generating report for attribute {attr}")
            reports[attr] = []

            attr_alloc = analysis.get_attr_allocations(
                allocations=allocations, attr_weights=self._meta[attr]
            )

            # Get current allocation from last (more recent) row
            last_alloc = attr_alloc.tail(1).values.tolist()[0]
            reports[attr].append(
                {
                    "type": "piechart",
                    "name": "Allocation",
                    "data": last_alloc,
                    "labels": list(attr_alloc.columns),
                }
            )

            # Allocation history
            reports[attr].append(
                {
                    "type": "areachart",
                    "name": "Allocation history",
                    "datasets": [
                        {"label": c, "data": attr_alloc[c].tolist()}
                        for c in attr_alloc.columns
                    ],
                    "labels": [d.strftime("%d %b %Y") for d in attr_alloc.index],
                }
            )

        return reports

    def _get_report_data(self):
        analysis = Analysis(
            prices=self._prices,
            conv_rates=self._dst_currency_rates,
            holdings_currencies=self._holding_to_currency,
        )

        # Balances graph
        allocations = analysis.get_allocations(
            balances=self.balances.values(), ndays=self.cfg.days
        )
        balances = {
            "datasets": [
                {
                    "label": "Balance",
                    "data": allocations.sum(axis=1).values.tolist(),
                },
            ],
            "labels": [d.strftime("%d %b %Y") for d in allocations.index],
        }

        # Earning graph
        earnings = analysis.get_earnings(
            allocations=allocations,
            transactions=self.cfg.transactions,
            ndays=self.cfg.days,
        )
        earnings = {
            "datasets": [
                {
                    "label": "Earnings",
                    "data": earnings.values.tolist(),
                },
            ],
            "labels": [d.strftime("%d %b %Y") for d in earnings.index],
        }

        # Generate report data for all known attributes
        attrs_report = self._get_attrs_report_data(
            analysis=analysis, allocations=allocations
        )

        return {"balances": balances, "earnings": earnings, "attrs": attrs_report}

    def gen_report(self, dst: str):
        """ Create an html report at the given destination """

        # First we copy the html dir into a tmp folder
        src = os.path.join(os.path.dirname(__file__), "html")
        tmp_dst = tempfile.TemporaryDirectory(suffix="." + __package__)
        shutil.copytree(src=src, dst=tmp_dst.name, dirs_exist_ok=True)

        # This is the data that we will feed to the report
        report_data = self._get_report_data()

        # Generate report
        jinja_env = Environment(
            loader=PackageLoader("inverno", "html"),
            autoescape=select_autoescape(["html", "xml"]),
        )

        index_path = os.path.join(tmp_dst.name, "index.html")
        index_template = jinja_env.get_template(name="index.html")
        index_template.stream(
            attrs=report_data["attrs"],
            balances=report_data["balances"],
            earnings=report_data["earnings"],
        ).dump(index_path)

        # Move report to dst
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(dst))
        shutil.move(src=tmp_dst.name, dst=dst)

    def _gen_animated_plot(self, df: pd.DataFrame, dst: str):
        fig = plt.figure()
        plt.xticks(rotation=45, ha="right", rotation_mode="anchor")
        plt.subplots_adjust(bottom=0.2, top=0.9)
        colors = [
            "red",
            "green",
            "blue",
            "orange",
            "black",
            "violet",
            "brown",
            "yellow",
            "purple",
        ]

        frames = min(self.cfg.days, df.index.size)
        start = df.index.size - frames

        def update_frame(i):
            plt.legend(df.columns)
            p = plt.plot(df[start : start + i].index, df[start : start + i].values)
            for i in range(len(df.columns)):
                p[i].set_color(colors[i % len(colors)])

        animator = ani.FuncAnimation(fig, update_frame, frames=frames, interval=300)
        animator.save(dst, fps=10)

    def _get_first_holdings(self):
        # Collect holdngs and earliest date
        holdings = {}
        for balance in self.balances.values():
            for holding in balance.holdings.values():
                if holding.get_key() not in holdings:
                    holdings[holding.get_key()] = {
                        "date": balance.date,
                        "holding": holding,
                    }
        return holdings

    def _get_prices(self):
        prices = []
        end_date = self.cfg.end_date
        for _, entry in self._first_holdings.items():
            price_history = self._get_holding_prices(
                start=entry["date"], end=end_date, holding=entry["holding"]
            )
            if price_history is not None:
                if all([np.isnan(p) for p in price_history.tail(7)]):
                    log_warning(
                        "Most recent price is older than one "
                        f"week for {entry['holding'].get_key()}"
                    )
                prices.append(price_history)

        # Put all together in a single dataframe
        prices = pd.concat(prices, axis=1, join="outer")

        # Use linear interpolation to cover NaNs
        prices = prices.interpolate(method="time", axis=0, limit_direction="both")

        return prices

    def _get_currencies(self):
        currencies = {}
        for entry in self._first_holdings.values():
            holding = entry["holding"]
            log_info(f"Getting currency for {holding.get_key()}")

            # Try from transactions
            for trs in self.cfg.transactions_by_holding(holding=holding):
                if trs.price is not None:
                    currencies[holding.get_key()] = trs.price.currency
                    break

            if holding.get_key() in currencies:
                continue

            # Try from prices
            c = self.cfg.get_currency(holding=holding)
            if c is not None:
                currencies[holding.get_key()] = c
                continue

            # Try from Yahoo Finance
            if holding.ticker is not None:
                ticker = yf.Ticker(holding.ticker)
                try:
                    c = Currency[ticker.info["currency"]]
                    currencies[holding.get_key()] = c
                    continue
                except KeyError:
                    pass

            raise ValueError(f"Couldn't determine currency for {holding.get_key()}")

        return currencies

    def _get_holding_prices(
        self, start: datetime, end: datetime, holding: Holding
    ) -> pd.Series:
        def _reindex(s: pd.Series):
            return s.reindex(
                index=pd.date_range(s.index.min(), s.index.max()),
                method="pad",
            )

        prices = self.cfg.get_prices(holding=holding, start=start, end=end)
        if prices is not None:
            log_info(f"Using user-provided prices for {holding.get_key()}")
            return _reindex(prices)

        # Try to fetch prices from Yahoo Finance
        if holding.ticker is not None:
            ticker = yf.Ticker(holding.ticker)
            prices = ticker.history(start=start, end=end, interval="1d")["Close"]
            prices.name = holding.get_key()
            if prices.size > 0:
                log_info(f"Using Yahoo Finance prices for {holding.get_key()}")
                return _reindex(prices)

        # Try to infer from transaction data
        log_warning(
            f"Inferring prices from transactions for {holding.get_key()}"
            ", prices could be inaccurate"
        )
        index = pd.date_range(start=start, end=end, freq="D")
        prices = pd.Series(index=index, dtype=np.float64)
        prices.name = holding.get_key()

        for trs in self.cfg.transactions_by_holding(holding=holding):
            if trs.price is None:
                continue
            prices[trs.date] = trs.price.amount

        return _reindex(prices)
