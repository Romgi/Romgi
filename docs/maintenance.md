# Maintaining this profile

The root `README.md` appears on [github.com/Romgi](https://github.com/Romgi) because this public repository has the same name as the account.

## Edit the profile

- Change the introduction, projects, toolkit, and links in `README.md`.
- Edit `assets/banner.svg` to change the header. It is a standalone vector image, with a reduced-motion fallback for its subtle route animation.
- Keep image paths relative to the repository so GitHub can serve them directly.

## Refresh the widgets

The **Refresh profile widgets** GitHub Actions workflow runs daily at 06:23 UTC. You can also run it manually from the repository's Actions tab. It uses the repository's built-in `GITHUB_TOKEN`; no personal access token or external widget service is required.

To generate locally with Python 3.13 or later:

```sh
python -m unittest discover -s tests -v
python scripts/update_widgets.py --user Romgi
```

The generator writes desktop and mobile SVGs plus `assets/data.md`, an accessible text version. The README selects the compact SVGs on narrow screens.

## What the numbers mean

Activity uses the exact past 365 days from the calendar visible to signed-out GitHub visitors. GitHub's native grid can include extra days to complete its first week, so its displayed total can differ slightly. The streak is the longest run of consecutive active days inside the widget's window.

Languages are weighted by code bytes reported by GitHub across owned, public, original, non-archived repositories. The profile repository is excluded. These percentages describe repository contents, not proficiency, time spent, or individual authorship.

The calendar request is unauthenticated. Private repository details and credentials are never written to the widgets. If fetching or parsing fails, the workflow fails and keeps the last valid committed assets. Check its run logs before retrying; GitHub calendar markup changes may require updating the parser.
