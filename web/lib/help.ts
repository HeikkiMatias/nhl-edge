// What each section of the dashboard shows and how to read it (#398), in one place: the ⓘ
// buttons' pop-ups and the Help tab read from here, and their terms from the glossary, so none
// of them disagree.
import type { TermKey } from "./glossary.ts";

export type Help = {
  title: string;
  what: string;
  read: string;
  source: string;
  terms: TermKey[];
};

export const HELP = {
  mmToday: {
    title: "Market making today",
    what:
      "Today's games as the simulator stands now, every 5 minutes while the watch runs: for " +
      "each team our fair value, Kalshi's best bid and ask, the bid we have resting and its " +
      "contracts, what we hold, and what the holding wins or loses at the result. Prices are " +
      "cents per $1 contract that pays out if its team wins, overtime and shootout included.",
    read:
      "Quoting: our bids are up. Pulled: Kalshi's price moved 3 cents or a new starter was " +
      "named, and the game waits for our fair value. Quotes stop 10 minutes before the start; " +
      "what we hold then rides on the result. Contracts on both teams pay a dollar a pair " +
      "whatever happens. The best and worst cases add up every game's result going our way or " +
      "against us.",
    source:
      "The simulator on the watch's polls of Kalshi's books, and our fair values (quote_live). " +
      "Owner-only. The nightly record is the record; this view is replaced at each cycle.",
    terms: ["marketMaking", "fairValue", "halfSpread", "fill", "pull", "twoSided"],
  },
  mmTotals: {
    title: "Market making so far",
    what:
      "The chosen variant's totals over every game day the simulator has kept: the days, the " +
      "fills and contracts, the share bought on both teams of a game, the paper profit, and " +
      "the profit expected by our fair value and by Pinnacle's close.",
    read:
      "Paper profit swings with game results and is mostly luck at this size. The expected " +
      "profit by Pinnacle's close is the honest test: if it stays below 0, quoting loses even " +
      "when the paper profit looks good.",
    source:
      "The simulator's figures per game day (quote_days), copied each night from its private " +
      "record. Owner-only.",
    terms: ["marketMaking", "fill", "twoSided", "edge", "makerFee"],
  },
  luck: {
    title: "Luck against edge",
    what:
      "Running totals in dollars over game days: the paper profit at settlement, the profit our " +
      "own fair value expected at each fill, and the profit Pinnacle's close says the fills " +
      "were worth. Each is after Kalshi's maker fee.",
    read:
      "The paper profit wanders with results. The other two are what the fills should earn on " +
      "average: when ours is above Pinnacle's, the sellers knew more than our model. The " +
      "close is known only for fills with a Pinnacle price after them; the legend gives the " +
      "share.",
    source: "The simulator's figures per game day (quote_days).",
    terms: ["edge", "luck", "makerFee"],
  },
  daily: {
    title: "Profit per game day",
    what: "Each game day's paper profit at settlement, in dollars, for the chosen variant.",
    read:
      "Above 0 a gain, below a loss. Expect big swings both ways at this size; a day whose " +
      "games aren't all settled yet is left out until they are.",
    source: "The simulator's figures per game day (quote_days).",
    terms: ["luck"],
  },
  perContract: {
    title: "Per contract",
    what:
      "What each contract bought earned on average, in cents: the spread at posting (our fair " +
      "value less the price), the markouts (Kalshi's price later, or Pinnacle's close, less " +
      "the price), and the profit at settlement after the fee.",
    read:
      "Above 0 is good for us. A spread above 0 with markouts below it is adverse selection: " +
      "we were filled when the market knew better. Each figure gets its 95% interval once its " +
      "fills span 4 weeks; until then it shows counts only.",
    source: "The simulator's nightly figures over every kept day (quote_reports).",
    terms: ["markout", "interval", "makerFee"],
  },
  variants: {
    title: "Every variant",
    what:
      "Each fair value at each half-spread, run side by side on the same books and days. Click " +
      "a row to show it above.",
    read:
      "Compare the profit expected by the close: it is the market's verdict. The paper profit " +
      "is mostly luck. A wider half-spread fills less but keeps more of each fill.",
    source: "The simulator's figures per game day (quote_days).",
    terms: ["fairValue", "halfSpread", "edge"],
  },
  holds: {
    title: "Hold rules after a pull",
    what:
      "How long quoting should wait after Kalshi's price moves 3 cents: four rules on our " +
      "model's fair value, each at each half-spread, scored from 2026-11-04.",
    read:
      "The measure is each rule's profit per game less wait's on the same games, at 1 cent. It " +
      "has its interval from game dates through 2026-11-23, with Holm's adjustment over the " +
      "three; then you pick a rule (#385). Before that, counts only.",
    source: "The hold rules' own record (quote_days and quote_reports, kalshi_quotes/holds/1).",
    terms: ["holdRule", "pull", "interval"],
  },
  requote: {
    title: "Requote after a starter change",
    what:
      "What to do when another starting goalie is named: wait for our model's next prediction " +
      "(today's rule), or a minute later post again at the model's value recomputed with the " +
      "new starter. Both on our model's fair value at each half-spread, scored from 2026-11-09 " +
      "(ADR 0059).",
    read:
      "The measure is requote's profit per game less wait's on the same games, at 1 cent. It " +
      "has its interval once the record spans 4 weeks; before that, counts only. A change " +
      "requote can't recompute is held as wait does, and the line under the table says why.",
    source: "The requote record (quote_days and quote_reports, kalshi_quotes/requote/1).",
    terms: ["requote", "pull", "interval"],
  },
  realBets: {
    title: "Today's real bets",
    what:
      "The bets the 12:45 ET decision picked, with the lowest price still worth taking and the " +
      "stake at your price: the ladder lists the stake from each price up.",
    read:
      "Record each bet once it is placed, or say why it was not. Until the game starts you can " +
      "correct an entry or undo it, with a note: each revision is kept beside the entry, never " +
      "an edit. From the start on, a mistake is corrected at the weekly check against the " +
      "venue's statement.",
    source: "The decision's Telegram alert; your entries and revisions go to the record.",
    terms: ["minPrice", "realStake", "realRecord", "realRevision", "practice"],
  },
  realRecord: {
    title: "Real record",
    what:
      "Every real bet and its paper twin: what was placed, at what price and stake, and how it " +
      "settled, with the real bankroll.",
    read: "The stop line at 80 € ends real bets until you decide to restart.",
    source: "The real bets' alerts, entries, revisions, settlements and corrections.",
    terms: ["realBankroll", "stopLine", "realTwin", "priceGap"],
  },
  paper: {
    title: "Paper trading",
    what:
      "The frozen policy's paper bets: CLV per bet, the measure, then the bankroll, the record " +
      "and the drawdown.",
    read:
      "CLV is judged with its interval at the formal review (ADR 0032); results are mostly luck " +
      "over this few bets (ADR 0034).",
    source: "The paper ledger and the nightly live report.",
    terms: ["paper", "clv", "interval", "luck"],
  },
  liveScores: {
    title: "Live games and our bets",
    what:
      "Tonight's games with their scores and clock, and the bet we hold on each: the one you " +
      "recorded (real, or practice before real stakes start), or else the paper bet, with its " +
      "price and stake, what it pays if it wins and what it costs if it loses.",
    read:
      "A game with our bet is outlined. Right now says whether the bet is leading, trailing or " +
      "tied, and what it would pay if the game ended now. A finished game shows the result, " +
      "provisional until the nightly settlement. The four totals add up the open bets and " +
      "today's finished ones, in euros for recorded bets and in units for paper ones.",
    source:
      "The NHL's public scoreboard every 30 seconds, and your recorded entries. Logos are the " +
      "NHL's, as its scoreboard serves them.",
    terms: ["live", "provisional", "units"],
  },
  slate: {
    title: "The slate",
    what:
      "Every game of the day: its decision, and the home team's chance to win by the market " +
      "(B1, the hollow dot) and by our blend (the filled dot) on one 0–100% scale.",
    read:
      "The further apart the dots, the more we disagree with the market; the numbers are " +
      "written beside them. Gaps over 8 points get a hand review (hard rule 8).",
    source: "The decision's ledger.",
    terms: ["b1", "blend", "gap", "status"],
  },
  results: {
    title: "Results and bankroll",
    what: "The paper bankroll after each settled day, the bets per day, and EV against CLV.",
    read: "Over this few bets results are mostly luck; CLV is the measure (ADR 0034).",
    source: "The paper ledger's settlements.",
    terms: ["units", "drawdown", "luck", "ev", "clv"],
  },
  ledger: {
    title: "Paper bets",
    what: "Every paper bet, newest first, with its result, CLV and fair move.",
    read: "Profit is luck at this size; CLV is the measure.",
    source: "The paper ledger and its settlements.",
    terms: ["result", "profit", "close", "clv"],
  },
  clv: {
    title: "Cumulative CLV per bet",
    what: "Each nightly report's CLV per bet to its date, under the current policy.",
    read:
      "Above 0 beats the closing market. Read the band, not the line: until the whole band " +
      "clears 0 nothing is decided.",
    source: "The nightly live report.",
    terms: ["clv", "interval"],
  },
  models: {
    title: "The model against the market",
    what:
      "Each nightly report's paired log loss of the blend against the recalibrated market " +
      "(BLEND − B1) and of the player layer against the team-and-goalie model (B3 − B2), with " +
      "the blend's calibration slope, over the frozen policy's game days.",
    read:
      "Below 0 favours the first model named; a calibration slope of 1 is calibrated. A point " +
      "is drawn only once its figure has its 95% interval, from 4 weeks of games; before that " +
      "the card says about when it comes. Read the band, not the line: no verdict before the " +
      "formal review.",
    source: "The nightly live report (live_reports), each night's figures to its date.",
    terms: ["comparison", "b1", "b3", "blend", "calibration", "interval"],
  },
  challengers: {
    title: "Challengers",
    what:
      "Every registered challenger: when it was registered, the game days and games it has " +
      "scored, a fitted one's training games toward its minimum, its primary measure, and the " +
      "way to the formal review.",
    read:
      "A difference in log loss is the challenger's less the other model's: below 0 favours the " +
      "challenger. Under 4 weeks a figure shows counts only. Nothing is a verdict before the " +
      "formal review, which tests them all together.",
    source: "The nightly live report's challengers section, from each day's run bundle.",
    terms: ["challenger", "primaryMeasure", "comparison", "interval", "formalReview"],
  },
  forecasters: {
    title: "The morning view and the close forecasters",
    what:
      "The morning view (ADR 0052) and the two close forecasters (ADR 0054, 0056), each with its " +
      "primary measure over time: the morning slope toward the close, each forecast's error " +
      "against no move, and version 2 against version 1.",
    read:
      "The morning slope above 0 means the market later prices in what B3 sees in the morning. " +
      "A forecaster's figure below 0 means it forecasts the move better than no move, or for " +
      "version 2, better than version 1. Counts only until 4 weeks.",
    source: "The nightly live report's morning view and close forecaster sections.",
    terms: ["primaryMeasure", "close", "interval", "formalReview"],
  },
  report: {
    title: "The live report",
    what:
      "The nightly report in full: coverage, CLV with the floor and bound, the model " +
      "comparisons, the blend's calibration, gaps for hand review and the alerts.",
    read:
      "Every figure has its 95% interval once it spans 4 weeks; no verdict before the formal " +
      "review on 2027-04-12 (ADR 0032). The charts above draw its figures over time.",
    source: "nhl live report, each night.",
    terms: ["interim", "coverage", "comparison", "calibration", "interval"],
  },
} satisfies Record<string, Help>;

export type HelpKey = keyof typeof HELP;
