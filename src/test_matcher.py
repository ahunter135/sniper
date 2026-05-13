from matcher import find_giveaway_word, is_free_giveaway_post


def test_basic_straight_quotes():
    assert find_giveaway_word('First to comment "me"') == "me"


def test_first_person_gets_it():
    assert find_giveaway_word('First person to comment "sold" gets it.') == "sold"


def test_curly_quotes():
    assert find_giveaway_word('First to comment “me” gets it') == "me"


def test_single_quotes():
    assert find_giveaway_word("First to say 'mine' takes it") == "mine"


def test_reversed_phrasing():
    assert find_giveaway_word('Drop "mine" first and it\'s yours') == "mine"


def test_reply_verb():
    assert find_giveaway_word('First to reply "yes" wins') == "yes"


def test_no_match_non_giveaway():
    assert find_giveaway_word('He said "hello" to me for the first time') is None


def test_no_match_no_quotes():
    assert find_giveaway_word("First to comment me gets it") is None


def test_no_match_unrelated_quote():
    assert find_giveaway_word('Check out the "Royal Oak" model, it\'s first in class') is None


def test_ignores_oversized_quote():
    assert find_giveaway_word('First to comment "a very long sentence that is clearly not a one-word response" wins') is None


def test_ignores_empty_quote():
    assert find_giveaway_word('First to comment "" gets it') is None


def test_real_watch_post_no_match():
    text = (
        "Omg. 33mm is lowkey my favorite size for AP. 56303st in the elusive white dial. "
        "I don’t think I’ve seen one before. Super nice patina on the hour markers and hands."
    )
    assert find_giveaway_word(text) is None


def test_hashtag_word():
    assert find_giveaway_word('First to comment "#me" gets it') == "#me"


# ---------- is_free_giveaway_post ----------

def test_free_int_price_zero():
    assert is_free_giveaway_post({"price": 0}) is True


def test_free_float_price_zero():
    assert is_free_giveaway_post({"price": 0.0}) is True


def test_free_string_price_zero():
    assert is_free_giveaway_post({"price": "$0"}) is True
    assert is_free_giveaway_post({"price": "0"}) is True
    assert is_free_giveaway_post({"price": "0.00"}) is True


def test_free_price_cents_zero():
    assert is_free_giveaway_post({"price_cents": 0}) is True


def test_paid_price_not_free():
    assert is_free_giveaway_post({"price": 1999}) is False
    assert is_free_giveaway_post({"price": "$1,999"}) is False
    assert is_free_giveaway_post({"price_cents": 199900}) is False


def test_no_price_field_not_free():
    # Announcement posts (discussion-type) typically have no price field —
    # must NOT trigger the fallback or we'd comment 'sold' on the SSK033
    # heads-up post itself.
    assert is_free_giveaway_post({"text": "Free Seiko deal coming!"}) is False
    assert is_free_giveaway_post({}) is False


def test_none_price_not_free():
    assert is_free_giveaway_post({"price": None}) is False


def test_bool_not_treated_as_price():
    # bool is a subclass of int in Python — guard against accidental True/False
    # being interpreted as 1/0.
    assert is_free_giveaway_post({"price": False}) is False
    assert is_free_giveaway_post({"price": True}) is False


# ---------- $0-in-text fallback (SSK033 case) ----------
# The actual SSK033 drop on 2026-05-04 had price=None, formatted_price=None,
# and "$0 + shipping" only in the body text. The strict price-only check
# missed it. These tests pin down the structural-guard fallback.

_SSK033_LISTING = {
    "id": 1548,
    "category": "buy_sell",
    "brand": {"id": 4, "name": "Seiko", "slug": "seiko"},
    "price": None,
    "formatted_price": None,
}

_ANNOUNCEMENT_POST = {
    # Daniel's pre-drop announcement post (id 1448 in production). Mentions
    # "$0" in passing but is not the actual giveaway listing.
    "id": 1448,
    "category": "buy_sell",
    "brand": None,
    "price": None,
}


def test_real_ssk033_listing_with_zero_dollar_in_text():
    text = "WatchLink Daily Deal 5/4/26. Seiko GMT SSK033. Brand new unworn. $0 + shipping"
    assert is_free_giveaway_post(_SSK033_LISTING, text) is True


def test_announcement_with_zero_dollar_no_brand_does_not_trigger():
    text = "Free Seiko Daily Deals. The Seiko will be listed for $0 at some point today/tonight."
    assert is_free_giveaway_post(_ANNOUNCEMENT_POST, text) is False


def test_zero_dollar_in_text_without_brand_does_not_trigger():
    # Defensive: random Daniel post mentioning "$0" but without listing
    # structure should not trigger.
    post = {"category": "buy_sell", "brand": None, "price": None}
    assert is_free_giveaway_post(post, "Hey here's a deal for $0 below cost") is False


def test_zero_dollar_in_discuss_category_does_not_trigger():
    post = {"category": "discuss", "brand": {"name": "Seiko"}, "price": None}
    assert is_free_giveaway_post(post, "$0 deal coming") is False


def test_no_text_argument_falls_back_to_price_only():
    # Backwards-compat: callers that don't pass text still work; price-only
    # logic applies.
    assert is_free_giveaway_post({"price": 0}) is True
    assert is_free_giveaway_post(_SSK033_LISTING) is False  # no text given


def test_paid_listing_with_brand_does_not_trigger():
    # The Tag Heuer 5/3 daily deal: $699 + brand set. Must NOT trigger.
    post = {"category": "buy_sell", "brand": {"name": "TAG Heuer"}, "price": 699}
    assert is_free_giveaway_post(post, "WatchLink Daily Deal $699 + label") is False


if __name__ == "__main__":
    import sys
    failed = 0
    total = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            total += 1
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{total - failed}/{total} passed")
    sys.exit(1 if failed else 0)
