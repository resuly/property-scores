from property_scores.common.au_state import detect_state


def test_canberra_is_act_but_queanbeyan_is_nsw():
    assert detect_state(-35.2809, 149.1300) == "ACT"
    assert detect_state(-35.3530, 149.2320) == "NSW"


def test_act_rural_interior_stays_act():
    assert detect_state(-35.5100, 149.0700) == "ACT"  # Tharwa district


def test_points_east_of_act_remain_nsw():
    assert detect_state(-35.2530, 149.4400) == "NSW"  # Bungendore district
