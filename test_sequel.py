import json
import urllib.request
import re

def get_sequel_info(mal_id: int):
    query = """
    query ($idMal: Int) {
      Media (idMal: $idMal, type: ANIME) {
        idMal
        episodes
        relations {
          edges {
            relationType
            node {
              idMal
              format
              episodes
              title {
                english
                romaji
              }
            }
          }
        }
      }
    }
    """
    try:
        url = "https://graphql.anilist.co"
        req_data = json.dumps({"query": query, "variables": {"idMal": mal_id}}).encode("utf-8")
        req = urllib.request.Request(url, data=req_data, headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            media = data.get("data", {}).get("Media")
            if not media:
                return None
            
            total_eps = media.get("episodes")
            edges = media.get("relations", {}).get("edges", [])
            for edge in edges:
                if edge.get("relationType") == "SEQUEL":
                    node = edge.get("node")
                    if node and node.get("idMal") and node.get("format") in ("TV", "TV_SHORT", "ONA"):
                        seq_title = node.get("title", {}).get("english") or node.get("title", {}).get("romaji")
                        return {
                            "sequel_mal_id": int(node["idMal"]),
                            "sequel_title": seq_title,
                            "parent_total_eps": total_eps
                        }
    except Exception as e:
        pass
    return None

print("Function defined!")
