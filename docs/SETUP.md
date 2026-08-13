# Setup Guide

## Prerequisites

- A GitHub account (free is fine)
- 5 minutes of time
- No coding experience needed

## Step-by-Step Setup

### 1. Download this repo as ZIP

Download all files from this folder and extract locally.

### 2. Create a new GitHub repository

1. Go to https://github.com/new
2. **Repository name:** `playztv-playlist` (or any name you like)
3. **Description:** `Auto-updating PlayZ TV playlist`
4. **Visibility:** **Public** (required for raw URL access without auth)
   - If you want Private, you'll need to use a GitHub token in Televizo (more complex)
5. **Initialize:** Uncheck all boxes (don't add README/license/.gitignore — we have our own)
6. Click **Create repository**

### 3. Push files to GitHub

Open a terminal in the extracted folder:

```bash
# Initialize git
git init
git branch -M main

# Add all files
git add .

# First commit
git commit -m "Initial commit — PlayZ TV auto-update playlist"

# Add your remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/playztv-playlist.git

# Push
git push -u origin main
```

You'll be asked for GitHub credentials. Use a Personal Access Token (PAT) as the password — create one at https://github.com/settings/tokens (classic token with `repo` scope).

### 4. Enable workflow permissions

1. Go to your repo → **Settings** → **Actions** → **General**
2. Scroll to **Workflow permissions**
3. Select **Read and write permissions**
4. Check ✅ **Allow GitHub Actions to create and approve pull requests**
5. Click **Save**

### 5. Trigger the first run

1. Go to **Actions** tab in your repo
2. Click **Update PlayZ TV Playlist** in the left sidebar
3. Click **Run workflow** (green button) → **Run workflow**
4. Wait ~1 minute, refresh the page
5. Click the latest run → watch it turn green ✓

### 6. Get your playlist URL

After the run completes, your playlists are at:

```
https://raw.githubusercontent.com/YOUR_USERNAME/playztv-playlist/main/output/playztv_hls_only.m3u
```

(Replace `YOUR_USERNAME` and `playztv-playlist` with your actual values.)

### 7. Add to Televizo

1. Open Televizo app
2. Go to **Settings** → **Playlist**
3. Click **Add playlist**
4. Select **M3U URL**
5. Paste:
   ```
   https://raw.githubusercontent.com/YOUR_USERNAME/playztv-playlist/main/output/playztv_hls_only.m3u
   ```
6. Click **OK**
7. Channels will load automatically

## Optional: Multiple Playlists

You can add multiple URLs to Televizo for different use cases:

| Use case | URL |
|---------|-----|
| All HLS streams | `.../output/playztv_hls_only.m3u` |
| Only live now | `.../output/playztv_live.m3u` |
| Only upcoming | `.../output/playztv_upcoming.m3u` |
| Everything (incl. DASH) | `.../output/playztv_master.m3u` |

## Optional: Change Update Frequency

Edit `.github/workflows/update-playlist.yml`:

```yaml
on:
  schedule:
    - cron: '0 * * * *'        # Default: every hour
    # - cron: '0 */2 * * *'    # Every 2 hours (saves Actions minutes)
    # - cron: '0 */6 * * *'    # Every 6 hours
    # - cron: '*/30 * * * *'   # Every 30 min (may hit rate limits)
```

Commit the change — next scheduled run will use the new interval.

## Verify It's Working

### Check 1: Workflow runs

Go to **Actions** tab — you should see a new run every hour (or your custom interval).

### Check 2: Output files updated

Go to `output/status.json` — `last_updated` should be recent (within the last hour).

### Check 3: Playlist loads in Televizo

Open Televizo → channels should appear with team names and live/upcoming status.

## Troubleshooting

### Workflow doesn't run

- Check **Actions** tab — is the workflow enabled? (Click "I understand my workflows, go ahead and enable them")
- Check workflow file is at `.github/workflows/update-playlist.yml` (case-sensitive)
- GitHub Actions cron can be delayed 5–15 min during peak load — wait a bit longer

### Workflow fails with "permission denied"

- Re-check Step 4 above (Workflow permissions set to "Read and write")
- Alternatively, add `GITHUB_TOKEN` env var explicitly in the workflow

### Playlists are empty

- Open the failed workflow run → check logs
- Common cause: API URL changed. The script auto-fetches the new URL from Firebase — should self-heal next run
- If decryption fails: AES keys may have changed in a new APK version. Re-extract from APK

### Televizo shows "Cannot load playlist"

- Verify the raw URL works in your browser — paste it in URL bar
- If 404: file path is wrong — check `output/` folder in your repo
- If 403: repo is Private. Make it Public, or use a token URL

### Streams don't play

- DASH/DRM streams require a player that supports them (VLC, ExoPlayer-based apps)
- Some streams are geoblocked — try a VPN matching the stream's country
- Live event streams may not exist until 30 min before kickoff — check `playztv_live.m3u` again later

## Manual Local Run (Optional)

If you want to test locally before pushing:

```bash
# Install Python deps
pip install -r scripts/requirements.txt

# Run
python scripts/playztv_auto_update.py

# Output goes to output/ folder
```

Useful for debugging or running on your own server with stricter cron timing.
