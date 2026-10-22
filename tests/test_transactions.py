"""League transactions: each player keeps his own position, action and bid."""

from fantasy_autopilot.yahoo.parse import parse_transactions_full

# Yahoo-like rows: the name link plus a second (empty) news link for each player.
HTML = """<table>
<tr><td><span>+</span><a href="https://sports.yahoo.com/nfl/players/33389">Kendrick Bourne</a>
<a class="news" href="https://sports.yahoo.com/nfl/players/33389/news"></a> NE - WR <span>Waiver ($12)</span>
<span>-</span><a href="https://sports.yahoo.com/nfl/players/40600">Braelon Allen</a>
<a href="https://sports.yahoo.com/nfl/players/40600/news"></a> NYJ - RB To Waivers</td>
<td><a href="/f1/43220/7">At least I'm not Dallas</a> Oct 14, 3:05 am</td></tr>
<tr><td><a href="https://sports.yahoo.com/nfl/teams/cin">Cincinnati</a> CIN - DEF Free Agent
<a href="https://sports.yahoo.com/nfl/teams/kc">Kansas City</a> KC - DEF To Waivers</td>
<td><a href="/f1/43220/7">At least I'm not Dallas</a> Oct 21, 9:10 am</td></tr>
<tr><td>Trade <a href="https://sports.yahoo.com/nfl/players/1">Malik Nabers</a> NYG - WR
<a href="https://sports.yahoo.com/nfl/players/2">Bucky Irving</a> TB - RB</td>
<td><a href="/f1/43220/7">Josh</a><a href="/f1/43220/13">Delio</a> Oct 6, 8:00 pm</td></tr>
</table>"""


def test_players_keep_their_own_fields():
    tx = parse_transactions_full(HTML)
    assert len(tx) == 3
    a = tx[0]
    assert a.team_ids == ["7"] and a.kind == "add/drop" and (a.month, a.day) == ("Oct", 14)
    bourne, allen = a.players
    assert (bourne.name, bourne.pos, bourne.action, bourne.bid) == ("Kendrick Bourne", "WR", "alta", 12)
    assert (allen.name, allen.pos, allen.action, allen.bid) == ("Braelon Allen", "RB", "baja", None)
    d = tx[1].players
    assert [(p.name, p.pos, p.action) for p in d] == [("Cincinnati", "DEF", "alta"), ("Kansas City", "DEF", "baja")]
    t = tx[2]
    assert t.kind == "trade" and t.team_ids == ["7", "13"] and all(p.action == "trade" for p in t.players)
