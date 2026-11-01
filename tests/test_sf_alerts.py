"""Unit tests for the shared alert formatting (config/triggers/scripts/sf_alerts.py).

Pure formatting only - no network calls, no RPC, no Slack: everything that talks to the
outside world in sf_alerts (TokenInfo, post_slack, AlertContext.w3) is reached through a
seam these tests substitute, so `npm test` needs neither web3 nor requests installed.

Run from the repo root (that's what `npm test` does): the trigger scripts address their
own directory by the same relative path OZ Monitor gives them at runtime, cwd=/app.
"""
import contextlib
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join("config", "triggers", "scripts"))

import sf_alerts  # noqa: E402

USDFC = "0x5553444643000000000000000000000000000000000000000000000000000000"
TFIL = "0x7446494c00000000000000000000000000000000000000000000000000000000"


class TestValueDecoding(unittest.TestCase):
    def test_decodes_bytes32_currency_codes(self):
        self.assertEqual(sf_alerts.decode_bytes32_string(USDFC), "USDFC")
        self.assertEqual(sf_alerts.decode_bytes32_string(TFIL), "tFIL")

    def test_decodes_currency_codes_without_0x_prefix(self):
        self.assertEqual(sf_alerts.decode_bytes32_string(USDFC[2:]), "USDFC")

    def test_to_int_accepts_every_shape_a_match_payload_uses(self):
        self.assertEqual(sf_alerts.to_int(1790294400), 1790294400)
        self.assertEqual(sf_alerts.to_int("1790294400"), 1790294400)
        self.assertEqual(sf_alerts.to_int("0x6AB0F480"), 0x6AB0F480)


class TestAmountFormatting(unittest.TestCase):
    def test_scales_18_decimal_amounts(self):
        # The real tFIL withdrawal that prompted this: 0.1 tFIL, not 10^17.
        self.assertEqual(sf_alerts.format_units(100000000000000000, 18), "0.1")

    def test_scales_6_decimal_amounts(self):
        self.assertEqual(sf_alerts.format_units(1250500000, 6), "1,250.5")

    def test_separates_thousands(self):
        self.assertEqual(
            sf_alerts.format_units(12345678900000000000000, 18), "12,345.6789"
        )

    def test_zero_decimal_tokens_are_left_whole(self):
        self.assertEqual(sf_alerts.format_units(1234, 0), "1,234")

    def test_whole_amounts_keep_no_fractional_part(self):
        self.assertEqual(sf_alerts.format_units(5 * 10**18, 18), "5")
        self.assertEqual(sf_alerts.format_units(0, 18), "0")

    def test_smallest_unit_is_never_rounded_away(self):
        # A rounded amount in a safety alert is a misreported amount - 1 wei must not
        # render as "0".
        self.assertEqual(sf_alerts.format_units(1, 18), "0.000000000000000001")

    def test_handles_uint256_sized_amounts(self):
        self.assertEqual(
            sf_alerts.format_units(2**256 - 1, 18),
            "115,792,089,237,316,195,423,570,985,008,687,907,853,269,984,665,640,564,039,457"
            ".584007913129639935",
        )


class TestTimeAndPriceFormatting(unittest.TestCase):
    def test_formats_maturity_timestamps_in_utc(self):
        # The maturities from the Auto Rolls alert in the original report.
        self.assertEqual(
            sf_alerts.format_timestamp(1790294400), "September 25, 2026 12:00 AM UTC"
        )
        self.assertEqual(
            sf_alerts.format_timestamp("1813881600"), "June 25, 2027 12:00 AM UTC"
        )

    def test_unit_prices_are_shown_as_percent_of_par(self):
        # Unit prices are integers out of Constants.PRICE_DIGIT (10000 = par).
        self.assertEqual(sf_alerts.format_unit_price(9550), "95.5%")
        self.assertEqual(sf_alerts.format_unit_price(10000), "100%")
        self.assertEqual(sf_alerts.format_unit_price(9999), "99.99%")


class TestMatchPayloadParsing(unittest.TestCase):
    def test_reads_every_matched_event_not_just_the_first(self):
        match_ = {
            "matched_on_args": {
                "events": [
                    {"args": [{"name": "ccy", "value": USDFC}]},
                    {"args": [{"name": "ccy", "value": TFIL}]},
                ]
            }
        }
        self.assertEqual(
            sf_alerts.matched_events(match_), [{"ccy": USDFC}, {"ccy": TFIL}]
        )

    def test_tolerates_a_match_with_no_decoded_args(self):
        self.assertEqual(sf_alerts.matched_events({}), [])
        self.assertEqual(sf_alerts.matched_events({"matched_on_args": {}}), [])

    def test_read_payload_splits_the_match_from_the_trigger_arguments(self):
        payload = {"monitor_match": {"EVM": {"network_slug": "x"}}, "args": ["itayose"]}
        match_, args = sf_alerts.read_payload(io.StringIO(json.dumps(payload)))
        self.assertEqual(match_, {"network_slug": "x"})
        self.assertEqual(args, ["itayose"])

    def test_read_payload_handles_a_trigger_with_no_arguments(self):
        payload = {"monitor_match": {"EVM": {}}, "args": None}
        _, args = sf_alerts.read_payload(io.StringIO(json.dumps(payload)))
        self.assertEqual(args, [])


def make_context():
    return sf_alerts.AlertContext(
        network_slug="filecoin_calibration",
        network_name="Filecoin Calibration",
        explorer="https://explorer.example",
        env_label="development",
    )


class TestAlertLayout(unittest.TestCase):
    def test_heading_is_uniform_across_concerns(self):
        self.assertEqual(
            make_context().heading_prefix, "[development:Filecoin Calibration]"
        )

    def test_transaction_hashes_render_as_explorer_links(self):
        tx_hash = "0x98e23f78c4b733a03a5d2edc4f64257030900bb9555db809cfbb6625cbac873d"
        self.assertEqual(
            make_context().tx_link(tx_hash),
            f"<https://explorer.example/tx/{tx_hash}|0x98e23f78…cbac873d>",
        )

    def test_addresses_render_as_explorer_links_keeping_the_full_address(self):
        user = "0xdba767f3dff3835bef5de1edee91a9901402ab21"
        self.assertEqual(
            make_context().address_link(user),
            f"<https://explorer.example/address/{user}|{user}>",
        )

    def test_renders_heading_summary_and_fields_in_order(self):
        text = sf_alerts.render(
            "[development:Filecoin Calibration]",
            "Big Withdrawal Detected",
            [("Currency", "tFIL"), ("Amount", "0.1 tFIL")],
            summary="1.96% of the tFIL held in TokenVault was withdrawn",
        )
        self.assertEqual(
            text,
            "*[development:Filecoin Calibration] Big Withdrawal Detected*\n"
            "1.96% of the tFIL held in TokenVault was withdrawn\n"
            "Currency: tFIL\n"
            "Amount: 0.1 tFIL",
        )

    def test_summary_is_optional(self):
        text = sf_alerts.render("[p:N]", "Itayose Detected", [("Currency", "USDFC")])
        self.assertEqual(text, "*[p:N] Itayose Detected*\nCurrency: USDFC")


ZERO_ADDRESS = "0x" + "0" * 40
VAULT = "0xc0ffee0000000000000000000000000000000000"
TOKEN = "0xd00d000000000000000000000000000000000000"


class FakeCall:
    def __init__(self, result):
        self._result = result

    def call(self, block_identifier=None):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeFunctions:
    """The two contract calls TokenInfo makes, recording that it made them."""

    def __init__(self, token_address, decimals, calls):
        self._token_address = token_address
        self._decimals = decimals
        self._calls = calls

    def getTokenAddress(self, ccy):  # noqa: N802 - mirrors the Solidity name
        self._calls.append("getTokenAddress")
        return FakeCall(self._token_address)

    def decimals(self):
        self._calls.append("decimals")
        return FakeCall(self._decimals)


class FakeContract:
    def __init__(self, functions):
        self.functions = functions


class FakeW3:
    """The thin slice of web3.py that TokenInfo uses."""

    def __init__(self, token_address=TOKEN, decimals=18, calls=None):
        self._token_address = token_address
        self._decimals = decimals
        self.calls = calls if calls is not None else []

    def to_checksum_address(self, address):
        return address

    @property
    def eth(self):
        return self

    def contract(self, address, abi):
        return FakeContract(
            FakeFunctions(self._token_address, self._decimals, self.calls)
        )


def make_token_info(**kwargs):
    return sf_alerts.TokenInfo(FakeW3(**kwargs), VAULT)


@contextlib.contextmanager
def quiet_stderr():
    """Swallows the diagnostic sf_alerts prints when an RPC call fails."""
    original, sys.stderr = sys.stderr, io.StringIO()
    try:
        yield
    finally:
        sys.stderr = original


class TestTokenInfo(unittest.TestCase):
    def test_formats_an_amount_in_the_currency_decimals(self):
        self.assertEqual(
            make_token_info(decimals=18).format_amount(TFIL, 100000000000000000),
            "0.1 tFIL",
        )
        self.assertEqual(
            make_token_info(decimals=6).format_amount(USDFC, 1250500000),
            "1,250.5 USDFC",
        )

    def test_resolves_each_currency_once_per_process(self):
        calls = []
        tokens = make_token_info(calls=calls)
        tokens.format_amount(TFIL, 1)
        tokens.format_amount(TFIL, 2)
        self.assertEqual(calls, ["getTokenAddress", "decimals"])

    def test_unregistered_currency_has_no_token_address(self):
        tokens = make_token_info(token_address=ZERO_ADDRESS)
        self.assertIsNone(tokens.token_address(TFIL))

    def test_unresolvable_decimals_fall_back_to_raw_rather_than_guessing(self):
        # Guessing 18 would under-report a USDFC amount by 10^12 and read as a
        # non-event; an explicitly raw figure is recoverable.
        tokens = make_token_info(token_address=ZERO_ADDRESS)
        self.assertEqual(
            tokens.format_amount(USDFC, 1250500000),
            "1,250,500,000 USDFC (raw base units - decimals unavailable)",
        )

    def test_a_missing_contract_address_file_still_sends_a_raw_amount(self):
        # contract_addresses.json absent or not covering this network: the alert must
        # still go out, with the amount labelled raw.
        ctx = make_context()
        with quiet_stderr():
            tokens = ctx.token_info()
        self.assertEqual(
            tokens.format_amount(TFIL, 100000000000000000),
            "100,000,000,000,000,000 tFIL (raw base units - decimals unavailable)",
        )

    def test_an_rpc_failure_leaves_the_alert_sendable(self):
        tokens = make_token_info(decimals=RuntimeError("rpc down"))
        with quiet_stderr():
            self.assertIsNone(tokens.decimals(TFIL))
        self.assertEqual(
            tokens.format_amount(TFIL, 100000000000000000),
            "100,000,000,000,000,000 tFIL (raw base units - decimals unavailable)",
        )


if __name__ == "__main__":
    unittest.main()
