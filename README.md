# Kalshi Bet Board (V1.0)

Every open Kalshi bet on one page, plus recently settled bets and your record. Click a card to open that market on Kalshi. GitHub Actions refreshes it every 15 minutes, with no Claude credits used.

## Setup

1. **Make a Kalshi API key:** go to Kalshi → Account & security → API Keys → Create key. Save the **Key ID** and download the **private key file** (Kalshi only shows it once).
2. **Add secrets:** go to repo Settings → Secrets and variables → Actions → New repository secret:
   - `KALSHI_API_KEY_ID` = the Key ID
   - `KALSHI_PRIVATE_KEY` = paste the whole private key file, including the `-----BEGIN ... KEY-----` lines
3. **Turn on Pages:** go to Settings → Pages → Source: **GitHub Actions**.
4. **First run:** go to Actions → "Update Kalshi bets" → Run workflow. The site goes live at `https://absinthe-007.github.io/Kalshi-Bet-Board/`.

## Notes

- The key only reads your data, because the script never calls an order endpoint.
- A public repo makes the Pages site public too. Anyone with the link can see your positions, but not your key.
- To refresh right away, run the workflow by hand from the Actions tab.
- Combo/parlay bets link to your Kalshi portfolio, because combos don't have their own market page.
