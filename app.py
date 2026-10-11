from downloader import SoulseekDownloader
import json
import re
import urllib.request

# Extractor de canciones de Spotify sin API Key
def obtener_canciones_sin_api(playlist_url):
    match = re.search(r'playlist/([a-zA-Z0-9]+)', playlist_url)
    if not match:
        raise ValueError("URL de playlist inválida.")
    
    playlist_id = match.group(1)
    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    
    req = urllib.request.Request(
        embed_url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    )
    
    html = urllib.request.urlopen(req).read().decode('utf-8')
    match_json = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
    
    canciones = []
    if match_json:
        data = json.loads(match_json.group(1))
        track_list = data['props']['pageProps']['state']['data']['entity']['trackList']
        
        for t in track_list:
            canciones.append({
                'nombre': t['title'],
                'artista': t['subtitle'],
                'album': t.get('entity', {}).get('release', {}).get('title', 'Desconocido'),
                'busqueda': f"{t['subtitle']} - {t['title']}"
            })
    return canciones

if __name__ == "__main__":
    url_prueba = input("Pega el enlace de una playlist pública de Spotify: ")
    canciones = obtener_canciones_sin_api(url_prueba)
    
    print(f"\n[OK] {len(canciones)} canciones cargadas.")
    
    # Inicializar el gestor de descargas
    gestor = SoulseekDownloader()
    
    # Abrir la carpeta de descargas al finalizar o al solicitarlo
    opcion_abrir = input("\n¿Quieres probar abrir la carpeta de descargas en el Explorador? (s/n): ")
    if opcion_abrir.lower() == 's':
        gestor.abrir_en_explorador()