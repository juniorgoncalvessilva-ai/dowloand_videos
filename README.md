# Arquivo YouTube

App local (Python) para baixar playlists/vídeos do YouTube e, se quiser, gerar a transcrição em `.txt`.

Os arquivos ficam em `downloads/`, nesta mesma pasta.

## Como abrir

No PowerShell, nesta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8765
```

Ou dê dois cliques em `run.bat`.

Abra [http://127.0.0.1:8765](http://127.0.0.1:8765).

## FFmpeg

Sem FFmpeg o download ainda funciona, mas a qualidade pode cair (o YouTube separa vídeo e áudio). Instale e reabra o terminal:

```powershell
winget install Gyan.FFmpeg
```

## Transcrição

1. Primeiro tenta as legendas do próprio YouTube (rápido).
2. Se não houver, usa Whisper local (`pip install faster-whisper`) ou a API da OpenAI se `OPENAI_API_KEY` estiver definida.

Use só conteúdo que você tem direito de guardar offline.
