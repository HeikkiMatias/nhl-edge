import numpy as np
import pytest

from nhl_edge.market.devig import DEFAULT_METHOD, Method, fair_probabilities, overround

# One book's two-way moneylines: a pick'em, favorites of growing strength, and a 3-way line.
TWO_WAY = np.array([[1.91, 1.91], [1.5, 2.6], [2.1, 1.8], [1.2, 4.5], [1.05, 11.0]])
THREE_WAY = np.array([[2.3, 4.1, 2.75], [1.6, 4.5, 4.8]])


@pytest.mark.parametrize("method", list(Method))
@pytest.mark.parametrize("prices", [TWO_WAY, THREE_WAY])
def test_probabilities_sum_to_1(method: Method, prices: np.ndarray) -> None:
    fair = fair_probabilities(prices, method)
    assert fair.shape == prices.shape
    np.testing.assert_allclose(fair.sum(axis=1), 1.0, atol=1e-12)
    assert (fair > 0).all()


@pytest.mark.parametrize("method", list(Method))
def test_symmetric_prices_give_even_odds(method: Method) -> None:
    np.testing.assert_allclose(fair_probabilities([1.91, 1.91], method), [0.5, 0.5])
    np.testing.assert_allclose(fair_probabilities([2.6, 2.6, 2.6], method), [1 / 3] * 3)


@pytest.mark.parametrize("method", list(Method))
def test_zero_margin_prices_stay_as_they_are(method: Method) -> None:
    np.testing.assert_allclose(fair_probabilities([1.5, 3.0], method), [2 / 3, 1 / 3])
    np.testing.assert_allclose(fair_probabilities([2.0, 2.0], method), [0.5, 0.5])


def test_multiplicative_scales_by_the_overround() -> None:
    # 1 / 1.5 + 1 / 2.6 = 1.051282: each side is divided by it.
    np.testing.assert_allclose(
        fair_probabilities([1.5, 2.6], Method.MULTIPLICATIVE), [0.634146, 0.365854], atol=1e-6
    )


@pytest.mark.parametrize("prices", [TWO_WAY, THREE_WAY])
def test_the_default_method_is_multiplicative(prices: np.ndarray) -> None:
    # ADR 0008.
    assert DEFAULT_METHOD is Method.MULTIPLICATIVE
    np.testing.assert_array_equal(
        fair_probabilities(prices), fair_probabilities(prices, Method.MULTIPLICATIVE)
    )


@pytest.mark.parametrize("prices", [*TWO_WAY, *THREE_WAY])
def test_power_raises_every_implied_probability_to_one_exponent(prices: np.ndarray) -> None:
    fair = fair_probabilities(prices, Method.POWER)
    exponents = np.log(fair) / np.log(1 / prices)
    np.testing.assert_allclose(exponents, exponents[0], rtol=1e-9)
    assert exponents[0] > 1


@pytest.mark.parametrize("prices", [*TWO_WAY, *THREE_WAY])
def test_shin_solves_its_model_with_one_insider_share(prices: np.ndarray) -> None:
    # Shin's p_i rearranges to z = (pi_i^2 / P - p_i^2) / (p_i (1 - p_i)), the same for every i.
    implied = 1 / prices
    fair = fair_probabilities(prices, Method.SHIN)
    z = (implied**2 / implied.sum() - fair**2) / (fair * (1 - fair))
    np.testing.assert_allclose(z, z[0], rtol=1e-9)
    assert 0 < z[0] < 1


def test_shin_on_two_outcomes_takes_the_same_margin_off_each_side() -> None:
    implied = 1 / TWO_WAY
    margin = implied.sum(axis=1, keepdims=True) - 1
    np.testing.assert_allclose(fair_probabilities(TWO_WAY, Method.SHIN), implied - margin / 2)


def test_power_and_shin_take_more_margin_off_the_longshot() -> None:
    multiplicative = fair_probabilities(TWO_WAY[1:], Method.MULTIPLICATIVE)
    for method in (Method.POWER, Method.SHIN):
        fair = fair_probabilities(TWO_WAY[1:], method)
        favorite = TWO_WAY[1:].argmin(axis=1)
        rows = np.arange(len(favorite))
        assert (fair[rows, favorite] > multiplicative[rows, favorite]).all()


@pytest.mark.parametrize("method", list(Method))
def test_markets_are_de_vigged_independently_and_in_price_order(method: Method) -> None:
    table = fair_probabilities(TWO_WAY, method)
    for row, prices in enumerate(TWO_WAY):
        np.testing.assert_allclose(fair_probabilities(prices, method), table[row])
        np.testing.assert_allclose(fair_probabilities(prices[::-1], method), table[row][::-1])


def test_overround() -> None:
    np.testing.assert_allclose(overround([1.91, 1.91]), 2 / 1.91)
    assert overround([1.91, 1.91]).shape == ()
    np.testing.assert_allclose(overround(TWO_WAY[:2]), [2 / 1.91, 1 / 1.5 + 1 / 2.6])


@pytest.mark.parametrize("method", list(Method))
@pytest.mark.parametrize(
    ("prices", "message"),
    [
        ([1.0, 5.0], "not above 1"),
        ([0.0, 1.9], "not above 1"),
        ([-110.0, 1.9], "not above 1"),  # an American price passed as decimal
        ([float("nan"), 1.9], "not above 1"),
        ([float("inf"), 1.9], "not above 1"),
        ([1.9], "at least two outcomes"),
        ([2.2, 2.2], "below 1"),  # the best price of two books, not one market
        ([[[1.9, 1.9]]], "3-D"),
    ],
)
def test_rejects_what_is_not_one_book_s_market(
    method: Method, prices: list[float], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        fair_probabilities(prices, method)
