from datetime import datetime

from tripowner.agent.guardrails import Guardrails
from tripowner.i18n import translate_known
from tripowner.planner import make_plan
from tripowner.sim.chaos import make_profile


def test_phrasebook_round_trip_keeps_landmark():
    en = "I am at Gate 2, Indiranagar Metro Station. Please come there."
    kn = translate_known(en, "en-IN", "kn-IN")
    assert kn and "Gate 2, Indiranagar Metro Station" in kn
    assert translate_known(kn, "kn-IN", "en-IN") == en


def test_guardrails_block_private_info_but_allow_landmark():
    g = Guardrails(make_profile("Indiranagar"), spend_cap_inr=600, max_bookings=4)
    assert not g.check_driver_message("Sure, call me on 9845012345").allowed
    assert not g.check_driver_message("my number is +91 98450 12345").allowed
    assert not g.check_driver_message("email asha.demo@example.com").allowed
    assert not g.check_driver_message("Come to Flat 7B please").allowed
    assert g.check_driver_message("I am at Gate 2, Indiranagar Metro Station.").allowed
    assert len(g.blocks) == 4


def test_booking_policy():
    g = Guardrails(make_profile("Indiranagar"), spend_cap_inr=600, max_bookings=4)
    assert g.check_booking(300, 0)[0] == "allow"
    assert g.check_booking(900, 0)[0] == "needs_approval"
    assert g.check_booking(300, 4)[0] == "deny"
    assert g.check_booking(300, 4, extra_allowed=2)[0] == "allow"


def test_plan_math():
    p = make_plan(datetime(2026, 10, 15, 10, 30), travel_min=30)
    assert p.leave_by == datetime(2026, 10, 15, 9, 55)   # 30 travel + 5 buffer
    assert p.book_at == datetime(2026, 10, 15, 9, 45)    # + 10 pickup allowance
