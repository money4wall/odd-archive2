# ODD ARCHIVE Cloud MVP

Cloud-first generator for English-language archival mini-documentaries. Video rendering runs on GitHub-hosted runners, not on your MacBook.

## Pipeline

1. Groq (`openai/gpt-oss-20b`) creates the episode outline and ten narration scenes.
2. Each finished scene is saved separately so a rerun can resume instead of regenerating completed scenes.
3. Pixabay API finds and downloads stock footage for each scene.
4. Edge TTS creates English narration.
5. FFmpeg renders and joins the 1080p scenes.
6. GitHub Actions publishes the MP4, thumbnail, metadata, and script as a downloadable artifact.

YouTube upload is an optional final step. It remains disabled until Google OAuth secrets are configured and the repository variable `AUTO_UPLOAD` is set to `true`. The workflow defaults to `unlisted` visibility.

## One-time setup

1. Upload the contents of this folder to the root of your GitHub repository, **keeping the folder structure**
   (`.github/workflows/generate.yml`, `tests/test_main.py`, `.gitignore`, `topics.txt`). Hidden folders such as
   `.github` are easiest to add with `git push` or GitHub Desktop; the web "Upload files" page can skip them.
2. Open **Settings → Secrets and variables → Actions → New repository secret**.
3. Add these secrets:
   - `GROQ_API_KEY` = your Groq API key.
   - `PIXABAY_API_KEY` = your Pixabay API key.
4. Open **Actions → Build ODD ARCHIVE episode → Run workflow**, enter a topic (or leave it empty to take the next one from `topics.txt`), and start the run.
5. When it finishes, download the `odd-archive-episode` artifact from the run page.

The workflow caches only the outline and scene JSON files (`work/*/outline.json`, `work/*/scenes`). The cache is saved in a step with `if: always()`, so a run that fails halfway still keeps its finished scenes, and the next run resumes from them.

## Optional YouTube publishing

1. In Google Cloud Console, enable YouTube Data API v3 and configure an OAuth client.
2. Obtain a refresh token with the scope `https://www.googleapis.com/auth/youtube.upload`.
3. Add repository secrets `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_REFRESH_TOKEN`.
4. Add repository Actions variable `AUTO_UPLOAD` with value `true`.
5. The workflow uploads as `unlisted` by default. Change `YOUTUBE_PRIVACY_STATUS` in the workflow to `private`, `unlisted`, or `public` if required.

YouTube quota, account restrictions, copyright checks, and monetization eligibility still apply. Uploading a video does not guarantee monetization.

## Validation

`python -m py_compile main.py upload_youtube.py` checks Python syntax. `python main.py --self-test` checks workflow structure, integration markers, and FFmpeg availability. A true end-to-end run still requires working API secrets and network access to Groq, Pixabay, and Edge TTS.

The scheduled workflow is Tuesday, Thursday, and Saturday at 10:00 UTC. Scheduled runs have no topic input, so they rotate through `topics.txt` by run number; edit that file to change the topics. For initial verification, run it manually from the Actions tab.

## Known external risks (not testable offline)

- Edge TTS sometimes answers 403 to GitHub datacenter IPs. If the voice step fails this way, re-run later or swap the TTS provider.
- Google OAuth consent screen in *Testing* status issues refresh tokens that expire after 7 days. Videos uploaded through an unaudited API project are forced to `private` by YouTube.
- Pixabay footage is generic stock; scene visuals will only loosely match narration. A 20B model can also state wrong facts, so review `script.txt` before publishing.
