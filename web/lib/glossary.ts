// What every field on the dashboard means, for a reader with no betting or statistics background
// (#210). One entry per term: each section's "What these mean" and the full glossary both read
// from here, so they never disagree.

export type Term = { term: string; meaning: string };

export const GLOSSARY = {
  paper: {
    term: "Paper betting",
    meaning:
      "Pretend bets: the model decides exactly as it would with money, and every decision is " +
      "logged before the game, but no money is staked. The point is to test the model honestly " +
      "on games it has never seen.",
  },
  units: {
    term: "Units and bankroll",
    meaning:
      "The paper money. The bankroll started at 100 units with the first bet on 2026-10-08. " +
      "Every stake is a small share of the bankroll at the time.",
  },
  price: {
    term: "Price",
    meaning:
      "Decimal odds offered by Pinnacle, a large bookmaker, at 12:45 ET on game day, when the " +
      "model decides. A 1-unit bet at 1.88 pays back 1.88 units if it wins (0.88 profit plus the " +
      "stake) and loses the 1 unit if it doesn't. A price of 2.00 is an even-money bet.",
  },
  b1: {
    term: "B1",
    meaning:
      "The bookmaker's own view of the home team's chance to win, read from its prices with the " +
      "bookmaker's margin taken out and slightly corrected. It's the baseline the model has to " +
      "beat: the market is very hard to beat.",
  },
  b3: {
    term: "B3",
    meaning:
      "The model's own chance for the home team, built from team strength, the likely goalies, " +
      "each skater's rating, rest and travel, and home ice. It doesn't look at the bookmaker's " +
      "prices.",
  },
  blend: {
    term: "Blend",
    meaning:
      "The final chance for the home team: the market's view (B1's) and the model's (B3's) " +
      "combined, with weights fitted on earlier seasons. Bets are chosen from the blend.",
  },
  gap: {
    term: "Gap",
    meaning:
      "The blend minus B1, in percentage points: how far the model disagrees with the market. " +
      "Above 8 points a person checks the game by hand, since a big disagreement is more often " +
      "a data error than an insight.",
  },
  status: {
    term: "Status",
    meaning:
      "What happened to a game's decision. 'predicted': the model made one. 'no Pinnacle midday " +
      "price' or 'no fresh price': no usable price, so no bet. 'started before the decision': " +
      "too late to bet. A game without a decision is never filled in later.",
  },
  side: {
    term: "Side",
    meaning: "The team the paper bet is on, to win the game.",
  },
  p: {
    term: "p",
    meaning: "The blend's chance that the side the bet is on wins.",
  },
  ev: {
    term: "EV (expected return)",
    meaning:
      "What the bet is expected to return per unit staked, at the price taken: chance × price " +
      "− 1. A 58% chance at 1.88 gives 0.58 × 1.88 − 1 = +9%: over many such bets, about 9 " +
      "cents won per unit staked, if the chance is right.",
  },
  hurdle: {
    term: "Hurdle",
    meaning:
      "The smallest EV that makes a bet: 2.5%, raised when the inputs are uncertain (unknown " +
      "goalies, injured players, rookies). It keeps the model from betting on edges too small " +
      "to trust.",
  },
  u: {
    term: "Uncertainty score (u)",
    meaning:
      "How unsure a game's inputs are: doubt over which goalies start, doubt over which " +
      "skaters play, and the share of ice time going to rookies or unknown players. It isn't " +
      "shown on the page, but it shapes the bets: above an average game's doubt, the hurdle " +
      "rises by 1 point and the stake shrinks for each step of extra doubt.",
  },
  stake: {
    term: "Stake",
    meaning:
      "Units bet: a quarter of what the Kelly formula would stake for that EV and price, at " +
      "most 1.5% of the bankroll on one bet and 5% on one day. Bigger edges get bigger stakes.",
  },
  result: {
    term: "Result",
    meaning:
      "Won or lost, on the full game: overtime and the shootout count, as for a moneyline bet. " +
      "Void when a game is postponed: the stake comes back.",
  },
  score: {
    term: "Score",
    meaning:
      "The final score, away team first. OT means it was decided in overtime, SO in a shootout " +
      "(the shootout's winner gets one extra goal).",
  },
  live: {
    term: "Live scores",
    meaning:
      "The NHL's own scoreboard for the slate's games, read every 30 seconds while a game is on: " +
      "the score (away team first), the period and the time left in it. The bet's team is in " +
      "bold. Scores are only shown here: they never enter the ledger, the settlement or the " +
      "model.",
  },
  provisional: {
    term: "Provisionally won or lost",
    meaning:
      "What the final score says about the bet, before it counts. The nightly run, around 05:00 " +
      "ET, settles every bet officially from the NHL's final result, overtime and the shootout " +
      "included; then the result reads Won or Lost. The two agree unless the NHL corrects a " +
      "result, which is rare.",
  },
  profit: {
    term: "Profit",
    meaning:
      "Units won or lost on the bet: stake × (price − 1) for a win, minus the stake for a loss.",
  },
  luck: {
    term: "Why results are mostly luck",
    meaning:
      "A good bet still loses often: a 58% bet loses 42% of the time. Over a few dozen bets, " +
      "the profit is mostly luck, in either direction, and a run of losses says little about " +
      "the model. That's why CLV, not profit, is the measure, and why nothing in the frozen " +
      "policy changes because of results (ADR 0034).",
  },
  drawdown: {
    term: "Drawdown",
    meaning:
      "How far the bankroll is below its highest point so far, in percent. At 20% below the " +
      "peak, the data and code get a review for errors. The model itself is never changed " +
      "because of results.",
  },
  close: {
    term: "Close",
    meaning:
      "Whether a closing price was found: the last Pinnacle price before the game started. " +
      "'proxy': found. 'stale' or 'missing': no usable price near the start. 'no pre-game " +
      "snapshot': the game started too soon after the decision to take one.",
  },
  clv: {
    term: "CLV (closing line value)",
    meaning:
      "How much better the price taken was than the fair closing price, per unit: price × " +
      "fair closing chance − 1. The closing price holds everything the market learned before " +
      "the game, so beating it again and again is the best early sign of a real edge, and far " +
      "more reliable than profit. It's the measure this test is judged on.",
  },
  fairMove: {
    term: "Fair move",
    meaning:
      "How the market's fair chance for the bet's side moved from the decision to the close. " +
      "Positive means the market moved toward the bet's side after it was taken.",
  },
  interval: {
    term: "Interval",
    meaning:
      "The range the true value plausibly lies in, 95% of the time: [low, high]. It comes from " +
      "resampling whole weeks of bets. If it includes 0, there is no evidence yet either way. " +
      "With fewer than four weeks only the counts are shown.",
  },
  interim: {
    term: "Interim, no verdict",
    meaning:
      "The live test is judged once, on 2027-04-12, after the regular season. Until then every " +
      "figure is for information: none of them is a verdict.",
  },
  coverage: {
    term: "Coverage and eligible bets",
    meaning:
      "A bet's CLV needs a closing price. Eligible bets are those whose game had a price " +
      "snapshot planned close to its start; the share of them with a closing price must be at " +
      "least 90% for the CLV to count.",
  },
  comparison: {
    term: "Model comparisons",
    meaning:
      "How well one model's chances fit the actual results compared with another's, as the " +
      "difference in log loss per game, a standard score for probabilities. Negative favours " +
      "the first model named.",
  },
  calibration: {
    term: "Calibration",
    meaning:
      "Whether the blend's chances come true as often as they say: of games it gives 60%, " +
      "about 60% should be won. The band is how far off it may be before the 2.5% hurdle " +
      "stops covering the error.",
  },
} satisfies Record<string, Term>;

export type TermKey = keyof typeof GLOSSARY;
