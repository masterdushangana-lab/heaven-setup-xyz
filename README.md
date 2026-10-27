# AnimeHeaven to Abyss.to Ingestion Pipeline (MAL-Indexed & Encrypted)

AnimeHeaven (`animeheaven.me`) වෙතින් episodes direct stream කර **100% Original Resolution** එකෙන්ම **Abyss.to** වෙත upload කර, **AES-256-GCM Password Encryption මඟින් lock කර** Cloudflare Pages UI එකක් හරහා පමණක් password දී decrypt කරගත හැකි අංගසම්පූර්ණ පද්ධතියකි.

---

## 🔒 Security Architecture (AES-256-GCM Locked)
- **Zero Plaintext in Git:** `uploaded_episodes.json` ගොනුව GitHub repo එකේ save වන්නේ **AES-256-GCM (PBKDF2-SHA256, 600,000 iterations)** මඟින් සම්පූර්ණයෙන්ම encrypt වී lock වීමෙන් පසුවයි.
- **Client-Side Decryption:** Password එක කිසිවිටෙකත් server එකට නොයයි. Web Crypto API (`SubtleCrypto`) මඟින් Browser එක තුළදීම decrypt වේ.
- **Multi-Level Deduplication:** Workflow එක run වන විටත් `MAPPING_PASSWORD` එකෙන් history කියවාගෙන duplicate නොවී ආරක්ෂා කරයි.

---

## 🌐 Cloudflare Portal Setup (පියවරෙන් පියවර)

1. [Cloudflare Dashboard](https://dash.cloudflare.com/) එකට log වන්න.
2. **Workers & Pages** -> **Create application** -> **Pages** -> **Connect to Git** ක්ලික් කරන්න.
3. ඔබගේ ഈ repository එක තෝරාගන්න.
4. Build Settings:
   - **Framework preset:** `None`
   - **Build output directory:** `cloudflare_ui`
5. Environment Variables:
   - **`GITHUB_REPO`**: `ඔබගේ-username/ඔබගේ-repo-name` (උදා: `john/heaven-setup-xyz`)
   - **`GITHUB_TOKEN`** (Private repo එකක් නම් පමණි): GitHub Personal Access Token (PAT).
6. **Save and Deploy** ක්ලික් කරන්න.
7. ලැබෙන Cloudflare link එකට ගොස් ඔබේ `MAPPING_PASSWORD` එක ලබා දී Dashboard එක unlock කරගන්න!

---

## ⚙️ GitHub Secrets Required
Repo **Settings** -> **Secrets and variables** -> **Actions**:
1. **`ABYSS_API_KEY`** (අනිවාර්යයි): Abyss.to API Key එක.
2. **`MAPPING_PASSWORD`** (අනිවාර්යයි): JSON ගොනුව Encrypt/Decrypt කිරීමට ඔබ තබන Master Password එක.
3. **`MAL_CLIENT_ID`** (විකල්පයි): Official MyAnimeList Client ID (නොමැති නම් AniMap auto භාවිතා වේ).
