from datetime import datetime

from tripowner.providers.osm_routes import OsmRoute, PlaceIndex, open_at, parse_hours, score_osm_route

FRI_11PM = datetime(2026, 10, 23, 23, 0)      # a Friday
SAT_1AM = datetime(2026, 10, 24, 1, 0)


def test_parse_common_formats():
    assert parse_hours("24/7")[3] == [(0, 1440)]
    w = parse_hours("Mo-Sa 09:00-21:00; Su off")
    assert w[0] == [(540, 1260)] and w[6] == []
    assert parse_hours("10:00-13:00,16:00-22:30")[2] == [(600, 780), (960, 1350)]
    assert parse_hours("Mo-Su 18:00-02:00")[4] == [(1080, 1560)]     # past midnight
    assert parse_hours("sunrise-sunset") is None                     # unknown format -> estimated instead


def test_open_at_uses_mapped_hours_and_past_midnight():
    assert open_at("Mo-Su 18:00-02:00", "restaurant", FRI_11PM) == (True, True)
    assert open_at("Mo-Su 18:00-02:00", "restaurant", SAT_1AM) == (True, True)    # still Friday night's hours
    assert open_at("Mo-Sa 09:00-21:00", "shop:clothes", FRI_11PM) == (False, True)


def test_estimates_by_kind_when_hours_unknown():
    assert open_at(None, "hospital", SAT_1AM) == (True, False)
    assert open_at(None, "shop:clothes", FRI_11PM) == (False, False)
    assert open_at(None, "restaurant", datetime(2026, 10, 23, 22, 0)) == (True, False)


def test_score_from_real_format_data():
    # A straight 2 km road east-west; a late restaurant and hospital near the start, nothing after.
    coords = [[77.60, 12.97], [77.6185, 12.97]]
    places = [{"lat": 12.9702, "lon": 77.6005, "kind": "restaurant", "hours": "Mo-Su 11:00-23:30"},
              {"lat": 12.9701, "lon": 77.6030, "kind": "hospital"},
              {"lat": 12.9701, "lon": 77.6100, "kind": "shop:clothes"},
              {"lat": 12.9700, "lon": 77.6150, "kind": "camera"},
              {"lat": 12.9800, "lon": 77.6150, "kind": "restaurant"}]     # 1 km away: not on the route
    s = score_osm_route(OsmRoute("Test Road", "nowhere", 2.0, 5, coords, 0.0), FRI_11PM, PlaceIndex(places))
    assert s.shops_total == 3 and s.shops_open == 2 and s.cctv == 1
    assert s.longest_gap_km == 1.5                 # open places only in the first 500 m
    assert s.hours_known_pct == 33 and s.source.startswith("OpenStreetMap")
