# AnimeHeaven to Abyss.to Ingestion Pipeline (MAL-Indexed)

AnimeHeaven (`animeheaven.me`) වෙතින් episodes direct stream කර **100% Original Resolution** එකෙන්ම **Abyss.to** වෙත upload කර, **MyAnimeList (MAL) ID** පදනම් කරගත් Tree Structure JSON එකකට Embed Links auto-map කරන ස්වයංක්‍රීය පද්ධතියකි.

## Tree Mapping Structure (`uploaded_episodes.json`)

```json
{
  "5114": {
    "mal_id": 5114,
    "title": "Fullmetal Alchemist: Brotherhood",
    "title_english": "Fullmetal Alchemist: Brotherhood",
    "title_japanese": "鋼の錬金術師 FULLMETAL ALCHEMIST",
    "media_type": "TV",
    "animeheaven_id": "32q3t",
    "animeheaven_url": "https://animeheaven.me/anime.php?32q3t",
    "episodes": {
      "1": {
        "slug": "abc123xyz",
        "embed_url": "https://abyss.to/abc123xyz",
        "filename": "Fullmetal_Alchemist_EP01.mp4",
        "size_mb": 263.08,
        "uploaded_at": 1729938000
      },
      "2": {
        "slug": "def456uvw",
        "embed_url": "https://abyss.to/def456uvw",
        "filename": "Fullmetal_Alchemist_EP02.mp4",
        "size_mb": 255.40,
        "uploaded_at": 1729938100
      }
    }
  }
}
```

## Features
- **Official MAL ID Mapping:** AnimeHeaven title එකෙන් official MyAnimeList ID එක, English title, Japanese title auto-resolve කර ගනී.
- **Episode Range Filtering:** `1-12`, `13-24`, `1-5,10-15` ආදී ලෙස කැමති ranges upload කරගත හැක.
- **Max 18 Parallel Runners:** එකවර episodes 18ක් දක්වා parallel upload වේ.
- **100% Original Resolution:** කිසිදු re-encode හෝ downscale වීමක් නැත.

## Setup Instructions

1. මෙම folder/zip එක GitHub repository එකකට push කරන්න.
2. Repo Settings -> **Secrets and variables** -> **Actions** වෙත ගොස් Secrets එක් කරන්න:
   - **`ABYSS_API_KEY`** (අනිවාර්යයි): ඔබගේ Abyss.to API Key එක.
   - **`MAL_CLIENT_ID`** (විකල්පයි / නිර්දේශිතයි): Official MyAnimeList Client ID එක (නොමැති නම් Jikan fallback API auto භාවිතා වේ).
3. Repo Settings -> **Actions** -> **General** -> **Workflow permissions** යටතේ **Read and write permissions** enable කරන්න.
