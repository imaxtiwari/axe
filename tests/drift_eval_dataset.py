"""Helper module for 50-label drift detection evaluation dataset."""

from __future__ import annotations

from axe.agents.drift_detect import Stance

# fmt: off
DRIFT_DATASET: list[dict[str, str]] = [
    # CONTRADICTS (20)
    {"assumption": "iPhone revenue grows 5% YoY", "signal": "Apple reported iPhone revenue declined 4% YoY this quarter.", "label": "CONTRADICTS"},
    {"assumption": "AWS revenue grows 20% YoY", "signal": "AWS revenue growth slowed to 12%, missing the 20% target.", "label": "CONTRADICTS"},
    {"assumption": "Gross margin expands to 45%", "signal": "Gross margin compressed to 41% due to higher component costs.", "label": "CONTRADICTS"},
    {"assumption": "Netflix adds 5 million subscribers", "signal": "Netflix lost 2 million subscribers globally in Q2.", "label": "CONTRADICTS"},
    {"assumption": "Tesla delivers 500k vehicles in Q3", "signal": "Tesla delivered 435k vehicles, below expectations.", "label": "CONTRADICTS"},
    {"assumption": "Meta ad revenue reaccelerates", "signal": "Meta ad revenue fell 6%, the second consecutive decline.", "label": "CONTRADICTS"},
    {"assumption": "NVIDIA data-center revenue doubles", "signal": "Data-center revenue was flat year over year at $14b.", "label": "CONTRADICTS"},
    {"assumption": "Guidance for 2024 EPS raised", "signal": "Management cut full-year EPS guidance citing macro weakness.", "label": "CONTRADICTS"},
    {"assumption": "Disney+ subscriber base stabilizes", "signal": "Disney+ lost 4 million subscribers in the quarter.", "label": "CONTRADICTS"},
    {"assumption": "Oil production increases 10%", "signal": "Production dropped 8% after field maintenance delays.", "label": "CONTRADICTS"},
    {"assumption": "Microsoft Azure growth stays above 30%", "signal": "Azure growth decelerated to 27%, below 30%.", "label": "CONTRADICTS"},
    {"assumption": "Shopify GMV grows 25%", "signal": "Shopify GMV shrank 5% due to slowing e-commerce demand.", "label": "CONTRADICTS"},
    {"assumption": "Air travel demand fully recovers", "signal": "Delta reported a 10% drop in bookings this month.", "label": "CONTRADICTS"},
    {"assumption": "Semiconductor inventory normalizes", "signal": "Industry inventory days surged to record highs.", "label": "CONTRADICTS"},
    {"assumption": "Fed cuts rates in Q2", "signal": "The Fed held rates steady and signaled no near-term cuts.", "label": "CONTRADICTS"},
    {"assumption": "Ethereum transaction fees stay low", "signal": "Average Ethereum gas fees spiked 300% this week.", "label": "CONTRADICTS"},
    {"assumption": "Cybersecurity spending accelerates", "signal": " CrowdStrike cited delayed enterprise security budgets.", "label": "CONTRADICTS"},
    {"assumption": "Grocery price inflation eases", "signal": "Kroger reported 8% food inflation, higher than last quarter.", "label": "CONTRADICTS"},
    {"assumption": "Consumer confidence improves", "signal": "The Conference Board consumer confidence index fell sharply.", "label": "CONTRADICTS"},
    {"assumption": "Housing starts recover", "signal": "Housing starts fell 12% in July to a 5-year low.", "label": "CONTRADICTS"},
    # CONFIRMS (15)
    {"assumption": "iPhone revenue grows 5% YoY", "signal": "Apple reported iPhone revenue grew 7% YoY this quarter.", "label": "CONFIRMS"},
    {"assumption": "AWS revenue grows 20% YoY", "signal": "AWS revenue rose 22%, topping the 20% growth target.", "label": "CONFIRMS"},
    {"assumption": "Gross margin expands to 45%", "signal": "Gross margin reached 46% on operating leverage.", "label": "CONFIRMS"},
    {"assumption": "Netflix adds 5 million subscribers", "signal": "Netflix added 6 million subscribers in Q2.", "label": "CONFIRMS"},
    {"assumption": "Tesla delivers 500k vehicles in Q3", "signal": "Tesla delivered 520k vehicles, beating the target.", "label": "CONFIRMS"},
    {"assumption": "Meta ad revenue reaccelerates", "signal": "Meta ad revenue grew 11%, marking a reacceleration.", "label": "CONFIRMS"},
    {"assumption": "NVIDIA data-center revenue doubles", "signal": "Data-center revenue surged 110% YoY to $30b.", "label": "CONFIRMS"},
    {"assumption": "Guidance for 2024 EPS raised", "signal": "Management raised full-year EPS guidance by 5%.", "label": "CONFIRMS"},
    {"assumption": "Disney+ subscriber base stabilizes", "signal": "Disney+ added 1 million subscribers, showing stability.", "label": "CONFIRMS"},
    {"assumption": "Oil production increases 10%", "signal": "Production rose 12% after new wells came online.", "label": "CONFIRMS"},
    {"assumption": "Microsoft Azure growth stays above 30%", "signal": "Azure grew 31% in constant currency.", "label": "CONFIRMS"},
    {"assumption": "Shopify GMV grows 25%", "signal": "Shopify GMV increased 27% YoY during the quarter.", "label": "CONFIRMS"},
    {"assumption": "Air travel demand fully recovers", "signal": "United Airlines reported record load factors in July.", "label": "CONFIRMS"},
    {"assumption": "Semiconductor inventory normalizes", "signal": "Industry inventory days declined to seasonal levels.", "label": "CONFIRMS"},
    {"assumption": "Fed cuts rates in Q2", "signal": "The Fed announced a 25bp rate cut at the June meeting.", "label": "CONFIRMS"},
    # NEUTRAL / UNCERTAIN (15)
    {"assumption": "iPhone revenue grows 5% YoY", "signal": "Apple announced a new purple iPhone color.", "label": "NEUTRAL"},
    {"assumption": "AWS revenue grows 20% YoY", "signal": "Amazon opened a new headquarters building.", "label": "NEUTRAL"},
    {"assumption": "Gross margin expands to 45%", "signal": "The company hired a new CFO.", "label": "NEUTRAL"},
    {"assumption": "Netflix adds 5 million subscribers", "signal": "Netflix is testing a new user interface.", "label": "NEUTRAL"},
    {"assumption": "Tesla delivers 500k vehicles in Q3", "signal": "Tesla CEO tweeted about AI optimism.", "label": "NEUTRAL"},
    {"assumption": "Meta ad revenue reaccelerates", "signal": "Meta launched a new corporate logo.", "label": "NEUTRAL"},
    {"assumption": "NVIDIA data-center revenue doubles", "signal": "NVIDIA announced a charity partnership.", "label": "NEUTRAL"},
    {"assumption": "Guidance for 2024 EPS raised", "signal": "The company scheduled an investor day next month.", "label": "NEUTRAL"},
    {"assumption": "Disney+ subscriber base stabilizes", "signal": "Disney renewed a licensing partnership.", "label": "NEUTRAL"},
    {"assumption": "Oil production increases 10%", "signal": "The CEO spoke at an energy conference.", "label": "NEUTRAL"},
    {"assumption": "Microsoft Azure growth stays above 30%", "signal": "Microsoft stock split rumors surfaced online.", "label": "NEUTRAL"},
    {"assumption": "Shopify GMV grows 25%", "signal": "Shopify published a new partner blog post.", "label": "NEUTRAL"},
    {"assumption": "Air travel demand fully recovers", "signal": "A new airline partnership was announced.", "label": "UNCERTAIN"},
    {"assumption": "Semiconductor inventory normalizes", "signal": "An analyst upgraded the sector to overweight.", "label": "UNCERTAIN"},
    {"assumption": "Housing starts recover", "signal": "Mortgage rates were unchanged this week.", "label": "UNCERTAIN"},
]
# fmt: on


def evaluate_stance(prediction: Stance, label: Stance) -> str:
    """Return confusion bucket for a single prediction."""
    if prediction == label:
        return "correct"
    if label == "CONTRADICTS":
        # Any non-CONTRADICTS is a false negative for contradiction recall
        return "fn_contradiction"
    if prediction == "CONTRADICTS" and label in ("CONFIRMS", "NEUTRAL", "UNCERTAIN"):
        return "fp_contradiction"
    return "incorrect_other"


__all__ = ["DRIFT_DATASET", "evaluate_stance"]
