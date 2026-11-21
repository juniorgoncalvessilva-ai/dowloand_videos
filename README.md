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

## Lives para cortes (criadores e autorizações)

O app é a entrada do pipeline de cortes. Uma live só entra nele se o criador tiver **autorização válida** cadastrada:

1. Em **Criadores e autorizações**, cadastre o criador e, de preferência, os canais dele (ex.: `https://www.youtube.com/@canal`). Com canais cadastrados, lives de outros canais são bloqueadas.
2. Registre a autorização: tipo (programa oficial de cortes, contrato ou licença aberta), prova (link, contrato ou print), regras, crédito obrigatório, plataformas permitidas, se pode monetizar e validade. Autorização vencida ou revogada não vale.
3. No download, escolha o criador em **Destino**. Sem autorização válida, o download é recusado.

Cada live fica em `downloads/lives/AAAA-MM-DD_<criador>_<título>/`:

```
bruto/                       vídeo original e legendas (nunca alterados)
bruto/<vídeo>.transcricao.json  falas com início, fim e palavras
bruto/<vídeo>.transcricao.srt
bruto/<vídeo>.txt            texto com [hh:mm:ss] em cada fala
live.json                    link, canal, data, criador, autorização usada e transcrição
```

O cadastro fica em `data/cortes.db` (SQLite, fora do git).

"Arquivo pessoal" continua baixando como antes, fora do pipeline de cortes.

## Transcrição

Toda transcrição sai com tempos por fala (e por palavra, quando o motor informa), em `.transcricao.json`, `.transcricao.srt` e `.txt`.

Motores (campo **Motor da transcrição**):

- **Automático**: legenda já baixada → Whisper local (faster-whisper) → Vosk → legendas do YouTube.
- **Rápido**: só legendas (baixada ou do YouTube). Bom para testar.
- **Whisper**: força o Whisper, melhor qualidade, mais lento em CPU.

O modelo do Whisper pode ser escolhido por variável de ambiente: `WHISPER_MODEL` (padrão `small`; `medium` ou `large-v3` são melhores e mais lentos), `WHISPER_DEVICE` (`auto`, `cpu`, `cuda`) e `WHISPER_COMPUTE` (padrão `int8` em CPU).

Quem fala (diarização) ainda não é identificado; o campo `falante` fica vazio.

Use só conteúdo que você tem direito de guardar offline e, para cortes, só de criadores que autorizaram.
