"""Records text: the instructions and my_usage say what a record is and why re-reads cost 0."""

from buildingactivity.mcp import server, tools


def test_instructions_explain_records_unit():
    assert "UTC calendar month" in server.INSTRUCTIONS
    assert "search_signals then get_signal charges once" in server.INSTRUCTIONS
    assert "my_usage" in server.INSTRUCTIONS


def test_my_usage_description():
    desc = tools.TOOLS["my_usage"].description
    assert "UTC calendar month" in desc
    assert "Free" in desc
    assert len(desc) <= 200
