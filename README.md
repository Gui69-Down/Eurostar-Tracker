# Eurostar Weekend Tracker 🚄

Suivi automatique des prix Eurostar pour un weekend Londres ⇄ Paris :
**vendredi soir aller, dimanche après-midi retour**, dans les deux sens, sur 16 semaines.

- Scrape 2x/jour via GitHub Actions (gratuit, aucun serveur)
- Historique des prix versionné dans le repo
- Alerte **Telegram** si un weekend passe sous le seuil (défaut : 110) ou chute de ≥15%
- Dashboard façon tableau des départs sur GitHub Pages

⚠️ Eurostar n'a pas d'API publique : ce projet scrape le site public avec un vrai navigateur (Playwright), à faible fréquence. Le site peut évoluer — si le scraper ne trouve plus de prix, il dépose un dump de debug dans `data/debug/` pour ajuster l'extraction.

## Installation (15 min)

### 1. Créer le repo
```bash
git init eurostar-tracker && cd eurostar-tracker
# copier tous les fichiers de ce dossier, puis :
git add . && git commit -m "init"
gh repo create eurostar-tracker --private --source=. --push
```
(ou créer le repo sur github.com et pousser classiquement)

### 2. Bot Telegram (5 min)
1. Sur Telegram, parle à **@BotFather** → `/newbot` → récupère le **token**.
2. Envoie un message à ton nouveau bot (n'importe quoi).
3. Ouvre `https://api.telegram.org/bot<TOKEN>/getUpdates` → note le `chat.id`.
4. Sur GitHub : **Settings → Secrets and variables → Actions** → ajoute :
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

### 3. Activer GitHub Pages
**Settings → Pages** → Source : *Deploy from a branch* → branche `main`, dossier `/docs`.
Ton dashboard sera sur `https://<toi>.github.io/eurostar-tracker/`.

### 4. Premier run
Onglet **Actions** → *Track Eurostar prices* → **Run workflow**.
Ensuite ça tourne tout seul à ~7h23 et 18h23.

## Réglages (`config.json`)
- `weekend_total_threshold` : seuil d'alerte sur le total A/R (110 par défaut)
- `friday_evening` / `sunday_return` : fenêtres horaires (17:00–21:30 et 14:00–21:30)
- `weeks_ahead` : horizon (16)
- Codes gares : si Eurostar change son format d'URL, fais une recherche manuelle sur eurostar.com et copie les paramètres de l'URL de résultats dans `search_url_template`.

## Test en local (optionnel)
```bash
pip install -r scraper/requirements.txt
playwright install chromium
python scraper/scrape.py
python scraper/alerts.py
open docs/index.html
```

## Limites connues
- Anti-bot : à 2 runs/jour depuis les IP GitHub, ça passe généralement, mais Eurostar peut bloquer ponctuellement. Si ça devient systématique : réduire la fréquence, ou faire tourner le script en local (cron sur ton Mac) où l'IP résidentielle passe mieux.
- Les prix affichés sont les minima relevés dans la fenêtre horaire — toujours re-vérifier sur eurostar.com avant de réserver.
- Usage personnel, faible volume. Ne pas transformer en service public/commercial (CGU Eurostar).
