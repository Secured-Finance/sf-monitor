"""Unit tests for the shared event-alert formatting
(config/triggers/scripts/event_alert.py).

These assert the exact Slack text for each concern against a real decoded match payload
shape, which is the thing that regressed into unreadability: a declarative trigger
printed a bytes32 currency, a Unix maturity and a base-unit amount verbatim.

No network: the TokenInfo resolver is passed in, so a concern's amount fields are
formatted against known decimals and a concern with no amount fields proves it never
asks for one (i.e. makes no RPC call at all).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join("config", "triggers", "scripts"))

import event_alert  # noqa: E402
import sf_alerts  # noqa: E402

USDFC = "0x5553444643000000000000000000000000000000000000000000000000000000"
USER = "0xdba767f3dff3835bef5de1edee91a9901402ab21"
TX = "0x98e23f78c4b733a03a5d2edc4f64257030900bb9555db809cfbb6625cbac873d"


def make_context():
    return sf_alerts.AlertContext(
        network_slug="filecoin_mainnet",
        network_name="Filecoin Mainnet",
        explorer="https://explorer.example",
        env_label="production",
    )


class FakeTokens:
    def __init__(self, decimals=18):
        self._decimals = decimals

    def format_amount(self, ccy_hex, raw):
        symbol = sf_alerts.decode_bytes32_string(ccy_hex)
        return f"{sf_alerts.format_units(raw, self._decimals)} {symbol}"


def no_tokens():
    raise AssertionError("this concern must not need a contract call to format")


def alert_text(concern_key, event_args, tokens=None):
    ctx = make_context()
    spec = event_alert.concerns(ctx)[concern_key]
    # The resolver is passed as a factory, the way event_alert hands it ctx.token_info -
    # so a concern needing no amount never builds one.
    resolver = (lambda: tokens) if tokens is not None else no_tokens
    fields = event_alert.build_fields(spec, event_args, resolver)
    fields.append(("Tx", ctx.tx_link(TX)))
    return sf_alerts.render(ctx.heading_prefix, spec["title"], fields)


class TestAutoRollsAlert(unittest.TestCase):
    # The alert from the original report, which read:
    #   Currency: 0x5553444643...0000
    #   Old Maturity: 1790294400
    #   Tx: 0x98e23f78...
    EVENT = {
        "ccy": USDFC,
        "oldMaturity": "1790294400",
        "newMaturity": "1813881600",
    }

    def test_renders_currency_maturities_and_a_linked_transaction(self):
        self.assertEqual(
            alert_text("autorolls", self.EVENT),
            "*[production:Filecoin Mainnet] Auto Roll Detected*\n"
            "Currency: USDFC\n"
            "Old Maturity: September 25, 2026 12:00 AM UTC\n"
            "New Maturity: June 25, 2027 12:00 AM UTC\n"
            f"Tx: <https://explorer.example/tx/{TX}|0x98e23f78…cbac873d>",
        )


class TestLiquidationAlert(unittest.TestCase):
    EVENT = {
        "user": USER,
        "collateralCcy": "0x7446494c00000000000000000000000000000000000000000000000000000000",
        "debtCcy": USDFC,
        "debtMaturity": "1790294400",
        "debtAmount": "1250500000",
    }

    def test_renders_both_currencies_the_maturity_and_a_scaled_amount(self):
        self.assertEqual(
            alert_text("liquidations", self.EVENT, FakeTokens(decimals=6)),
            "*[production:Filecoin Mainnet] Liquidation Detected*\n"
            f"User: <https://explorer.example/address/{USER}|{USER}>\n"
            "Collateral Currency: tFIL\n"
            "Debt Currency: USDFC\n"
            "Debt Maturity: September 25, 2026 12:00 AM UTC\n"
            "Debt Amount: 1,250.5 USDFC\n"
            f"Tx: <https://explorer.example/tx/{TX}|0x98e23f78…cbac873d>",
        )

    def test_amounts_are_denominated_in_the_debt_currency(self):
        # Not the collateral currency, and not the event's first bytes32 arg.
        text = alert_text("liquidations", self.EVENT, FakeTokens(decimals=6))
        self.assertIn("Debt Amount: 1,250.5 USDFC", text)


class TestItayoseAlert(unittest.TestCase):
    EVENT = {
        "ccy": USDFC,
        "maturity": "1790294400",
        "openingUnitPrice": "9550",
        "lastLendUnitPrice": "9560",
        "lastBorrowUnitPrice": "9540",
        "offsetAmount": "100000000000000000",
    }

    def test_renders_unit_prices_as_percentages_and_the_amount_scaled(self):
        self.assertEqual(
            alert_text("itayose", self.EVENT, FakeTokens(decimals=18)),
            "*[production:Filecoin Mainnet] Itayose Detected*\n"
            "Currency: USDFC\n"
            "Maturity: September 25, 2026 12:00 AM UTC\n"
            "Opening Unit Price: 95.5%\n"
            "Last Lend Unit Price: 95.6%\n"
            "Last Borrow Unit Price: 95.4%\n"
            "Offset Amount: 0.1 USDFC\n"
            f"Tx: <https://explorer.example/tx/{TX}|0x98e23f78…cbac873d>",
        )


class TestFieldSelection(unittest.TestCase):
    def test_every_concern_renders_the_same_heading_shape(self):
        ctx = make_context()
        for spec in event_alert.concerns(ctx).values():
            text = sf_alerts.render(ctx.heading_prefix, spec["title"], [])
            self.assertTrue(text.startswith("*[production:Filecoin Mainnet] "))
            self.assertTrue(text.endswith("Detected*"))

    def test_an_arg_missing_from_the_payload_is_skipped_not_fatal(self):
        # A partial payload must still deliver what it does carry - an alert that
        # raises is an alert nobody sees.
        text = alert_text("autorolls", {"ccy": USDFC, "newMaturity": "1813881600"})
        self.assertIn("Currency: USDFC", text)
        self.assertNotIn("Old Maturity", text)


if __name__ == "__main__":
    unittest.main()
