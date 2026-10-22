"""permit_row and change_row surface the API's agency_name next to the agency key."""
from buildingactivity.mcp import tools as T


def test_permit_row_keeps_agency_and_agency_name_in_order():
    p = {"id": "pmt_1", "permit_number": "BP-1", "agency": "bridgeport_tx",
         "agency_name": "Bridgeport, CT", "address": "1 Main St"}
    row = T.permit_row(p)
    keys = list(row)
    assert row["agency"] == "bridgeport_tx"
    assert row["agency_name"] == "Bridgeport, CT"
    assert keys.index("agency_name") == keys.index("agency") + 1


def test_permit_row_without_agency_name_has_no_key():
    p = {"id": "pmt_2", "permit_number": "BP-2", "agency": "bridgeport_tx"}
    row = T.permit_row(p)
    assert "agency_name" not in row


def test_change_row_with_permit_carries_agency_name():
    e = {"seq": 1, "type": "permit.updated", "permit_id": "pmt_3",
         "permit": {"permit_number": "BP-3", "agency": "brookpark_tyler_esuite_oh",
                    "agency_name": "Brooklyn Park, MN", "address": "2 Main St"}}
    row = T.change_row(e)
    assert row["agency"] == "brookpark_tyler_esuite_oh"
    assert row["agency_name"] == "Brooklyn Park, MN"
