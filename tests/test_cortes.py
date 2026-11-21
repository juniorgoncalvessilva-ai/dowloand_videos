import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import autorizacoes as aut
import core
import transcricao as tr


class AutorizacaoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "cortes.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _criador(self, canais=None):
        return aut.criar_criador("Podcast X", canais or [], db_path=self.db)

    def test_sem_autorizacao_bloqueia(self):
        c = self._criador()
        with self.assertRaises(aut.AutorizacaoError):
            aut.autorizacao_vigente(c["id"], db_path=self.db)

    def test_autorizacao_valida_libera(self):
        c = self._criador()
        aut.adicionar_autorizacao(
            c["id"], "programa", "https://exemplo.com/programa", programa="Cortes oficiais",
            plataformas=["youtube", "tiktok"], valido_ate="2099-12-31", db_path=self.db,
        )
        criador, a = aut.autorizacao_vigente(c["id"], db_path=self.db)
        self.assertEqual(criador["nome"], "Podcast X")
        self.assertEqual(a["programa"], "Cortes oficiais")
        self.assertEqual(a["plataformas"], ["youtube", "tiktok"])

    def test_vencida_e_futura_nao_valem(self):
        c = self._criador()
        aut.adicionar_autorizacao(c["id"], "contrato", "contrato.pdf", valido_ate="2026-01-31", db_path=self.db)
        aut.adicionar_autorizacao(c["id"], "contrato", "contrato2.pdf", valido_de="2027-01-01", db_path=self.db)
        with self.assertRaises(aut.AutorizacaoError):
            aut.autorizacao_vigente(c["id"], db_path=self.db, hoje=date(2026, 11, 21))

    def test_revogar(self):
        c = self._criador()
        a = aut.adicionar_autorizacao(c["id"], "licenca", "https://creativecommons.org", db_path=self.db)
        self.assertTrue(a["valida"])
        self.assertFalse(aut.revogar_autorizacao(a["id"], db_path=self.db)["valida"])
        self.assertFalse(aut.obter_criador(c["id"], db_path=self.db)["autorizado"])

    def test_validacoes(self):
        c = self._criador()
        with self.assertRaises(aut.AutorizacaoError):
            aut.adicionar_autorizacao(c["id"], "pirata", "x", db_path=self.db)
        with self.assertRaises(aut.AutorizacaoError):
            aut.adicionar_autorizacao(c["id"], "programa", "  ", db_path=self.db)
        with self.assertRaises(aut.AutorizacaoError):
            aut.adicionar_autorizacao(c["id"], "programa", "x", plataformas=["orkut"], db_path=self.db)
        with self.assertRaises(aut.AutorizacaoError):
            aut.adicionar_autorizacao(
                c["id"], "programa", "x", valido_de="2026-05-01", valido_ate="2026-01-01", db_path=self.db
            )
        with self.assertRaises(aut.AutorizacaoError):
            aut.adicionar_autorizacao(999, "programa", "x", db_path=self.db)

    def test_normalizar_canal(self):
        self.assertEqual(aut.normalizar_canal("https://www.youtube.com/@Canal/videos"), "@canal")
        self.assertEqual(aut.normalizar_canal("https://youtube.com/channel/UCabc123"), "ucabc123")
        self.assertEqual(aut.normalizar_canal("UCabc123"), "ucabc123")
        self.assertEqual(aut.normalizar_canal("https://www.twitch.tv/Fulano"), "twitch:fulano")

    def test_canal_confere(self):
        c = self._criador(["https://www.youtube.com/@podcastx"])
        self.assertTrue(aut.canal_confere(c, {"uploader_id": "@PodcastX", "channel_id": "UCzzz"}))
        self.assertTrue(aut.canal_confere(c, {"uploader_url": "https://www.youtube.com/@podcastx"}))
        self.assertFalse(aut.canal_confere(c, {"uploader_id": "@outro", "channel_id": "UCzzz"}))
        sem_canal = aut.criar_criador("Sem canal", db_path=self.db)
        self.assertTrue(aut.canal_confere(sem_canal, {"uploader_id": "@qualquer"}))


AUTO_VTT = """WEBVTT
Kind: captions
Language: pt

00:00:01.000 --> 00:00:03.500 align:start position:0%
bom<00:00:01.400><c> dia</c><00:00:01.900><c> pessoal</c>

00:00:03.500 --> 00:00:03.510 align:start position:0%
bom dia pessoal

00:00:03.510 --> 00:00:06.000 align:start position:0%
bom dia pessoal
hoje<00:00:04.000><c> tem</c><00:00:04.300><c> live</c>
"""


class TranscricaoTests(unittest.TestCase):
    def test_legenda_automatica_com_tempos(self):
        segs = tr.parse_legenda(AUTO_VTT)
        self.assertEqual([s.texto for s in segs], ["bom dia pessoal", "hoje tem live"])
        self.assertEqual(segs[0].inicio, 1.0)
        self.assertEqual(segs[1].inicio, 3.51)
        palavras = [(p.palavra, p.inicio) for p in segs[0].palavras]
        self.assertEqual(palavras, [("bom", 1.0), ("dia", 1.4), ("pessoal", 1.9)])
        self.assertEqual(segs[1].palavras[-1].fim, 6.0)

    def test_srt_simples(self):
        srt = "1\n00:00:00,500 --> 00:00:02,000\nOlá\n\n2\n00:01:02,250 --> 00:01:04,000\nmundo\n"
        segs = tr.parse_legenda(srt)
        self.assertEqual([(s.inicio, s.fim, s.texto) for s in segs], [(0.5, 2.0, "Olá"), (62.25, 64.0, "mundo")])

    def test_snippets_youtube(self):
        segs = tr.segmentos_de_snippets([{"text": "oi", "start": 1.5, "duration": 2}, {"text": " ", "start": 4, "duration": 1}])
        self.assertEqual([(s.inicio, s.fim, s.texto) for s in segs], [(1.5, 3.5, "oi")])

    def test_vosk_palavras(self):
        segs = tr.segmentos_de_vosk([
            {"text": "olá mundo", "result": [
                {"word": "olá", "start": 0.2, "end": 0.5, "conf": 1},
                {"word": "mundo", "start": 0.6, "end": 1.0, "conf": 1},
            ]},
            {"text": ""},
        ])
        self.assertEqual(len(segs), 1)
        self.assertEqual((segs[0].inicio, segs[0].fim), (0.2, 1.0))

    def test_formatos(self):
        self.assertEqual(tr.fmt_ts(3723.456), "01:02:03,456")
        self.assertEqual(tr.fmt_relogio(3723.9), "01:02:03")
        segs = [tr.Segmento(1.0, 2.5, "oi"), tr.Segmento(61, 62, "tchau")]
        self.assertIn("00:00:01,000 --> 00:00:02,500", tr.para_srt(segs))
        self.assertIn("[00:01:01] tchau", tr.para_txt(segs, ""))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(core, "DOWNLOADS", self.root),
            mock.patch.object(core, "LIVES", self.root / "lives"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_id_no_nome_do_arquivo(self):
        self.assertIn("[%(id)s]", core.OUTTMPL_ARQUIVO)
        self.assertIn("[%(id)s]", core.OUTTMPL_LIVE)
        self.assertEqual(core.video_id_do_nome(Path("001 - Aula [6D9TZyQxrC0].mp4")), "6D9TZyQxrC0")

    def test_legenda_gerada_nao_vira_fonte(self):
        video = self.root / "Live [abcdefghijk].mp4"
        video.write_bytes(b"x")
        (self.root / "Live [abcdefghijk].transcricao.srt").write_text("1\n", encoding="utf-8")
        self.assertIsNone(core._sidecar_legenda(video))
        (self.root / "Live [abcdefghijk].pt.vtt").write_text(AUTO_VTT, encoding="utf-8")
        self.assertEqual(core._sidecar_legenda(video).name, "Live [abcdefghijk].pt.vtt")

    def test_transcricao_atualiza_live_json(self):
        criador = {"id": 1, "nome": "Podcast X", "canais": []}
        autorizacao = {"id": 7, "tipo": "programa", "programa": "Cortes", "prova": "link", "plataformas": ["youtube"]}
        info = {"id": "abcdefghijk", "title": "Live 1", "upload_date": "20261120", "channel": "Podcast X",
                "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk", "was_live": True}
        pasta = core.pasta_da_live(criador, info)
        self.assertEqual(pasta.name, "2026-11-20_Podcast X_Live 1")
        bruto = pasta / "bruto"
        bruto.mkdir(parents=True)
        video = bruto / "Live 1 [abcdefghijk].mp4"
        video.write_bytes(b"x")
        (bruto / "Live 1 [abcdefghijk].pt.vtt").write_text(AUTO_VTT, encoding="utf-8")
        core._escrever_json(pasta / "live.json", core.montar_live_json(info, criador, autorizacao, video, pasta))

        txt, fonte = core.transcribe_file(video, None, ["pt"], engine="rapido")
        self.assertEqual(fonte, "legenda")
        self.assertTrue(txt.read_text(encoding="utf-8").splitlines()[-1].startswith("[00:00:03] hoje tem live"))
        dados = json.loads((bruto / "Live 1 [abcdefghijk].transcricao.json").read_text(encoding="utf-8"))
        self.assertEqual(dados["segmentos"][0]["inicio"], 1.0)
        live = json.loads((pasta / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(live["criador"]["nome"], "Podcast X")
        self.assertEqual(live["autorizacao"]["programa"], "Cortes")
        self.assertEqual(live["arquivo"], "bruto/Live 1 [abcdefghijk].mp4")
        self.assertTrue(live["foi_live"])
        self.assertEqual(live["transcricao"]["fonte"], "legenda")
        self.assertEqual(live["transcricao"]["json"], "bruto/Live 1 [abcdefghijk].transcricao.json")

    def test_sem_motor_disponivel_explica(self):
        video = self.root / "x.mp4"
        video.write_bytes(b"x")
        with mock.patch.object(tr, "whisper_disponivel", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                core.transcribe_file(video, None, ["pt"], engine="whisper")
        self.assertIn("faster-whisper", str(ctx.exception))

    def test_start_job_bloqueia_sem_autorizacao(self):
        with mock.patch.object(aut, "DB_PATH", self.root / "cortes.db"):
            c = aut.criar_criador("Sem permissão")
            with mock.patch.object(core, "inspect_url") as inspect:
                with self.assertRaises(aut.AutorizacaoError):
                    core.start_job("https://youtu.be/abcdefghijk", None, "best", False, None, ["pt"], creator_id=c["id"])
                inspect.assert_not_called()

    def _rodar_job(self, criador, info):
        autorizacao = {"id": 3, "tipo": "programa", "programa": "Cortes", "prova": "link", "plataformas": []}

        def fake_download(opts, target):
            destino = Path(opts["outtmpl"]).parent
            (destino / f"{info['title']} [{info['id']}].mp4").write_bytes(b"video")
            (destino / f"{info['title']} [{info['id']}].pt.vtt").write_text(AUTO_VTT, encoding="utf-8")

        job = core.Job(id="j", title="t", total=1)
        video = {"id": info["id"], "title": info["title"], "url": "https://youtu.be/" + info["id"]}
        with mock.patch.object(core, "_ydl_info", return_value=info), \
                mock.patch.object(core, "_ydl_download", side_effect=fake_download), \
                mock.patch.object(core.time, "sleep"):
            core._run_job(job, [video], "best", True, None, ["pt"], core.LIVES,
                          criador=criador, autorizacao=autorizacao, engine="rapido")
        return job

    def test_job_de_live_completo(self):
        criador = {"id": 1, "nome": "Podcast X", "canais": ["@podcastx"]}
        info = {"id": "abcdefghijk", "title": "Live 1", "upload_date": "20261120", "uploader_id": "@PodcastX"}
        job = self._rodar_job(criador, info)
        self.assertEqual(job.errors, [])
        self.assertEqual(job.status, "done")
        pasta = self.root / "lives" / "2026-11-20_Podcast X_Live 1"
        live = json.loads((pasta / "live.json").read_text(encoding="utf-8"))
        self.assertEqual(live["arquivo"], "bruto/Live 1 [abcdefghijk].mp4")
        self.assertEqual(live["transcricao"]["fonte"], "legenda")
        self.assertEqual(job.files[0]["live"], "lives/2026-11-20_Podcast X_Live 1/live.json")

    def test_job_bloqueia_canal_errado(self):
        criador = {"id": 1, "nome": "Podcast X", "canais": ["@podcastx"]}
        info = {"id": "abcdefghijk", "title": "Live 1", "uploader_id": "@outrocanal", "channel": "Outro"}
        job = self._rodar_job(criador, info)
        self.assertEqual(job.status, "error")
        self.assertIn("bloqueado", job.errors[0])
        self.assertFalse((self.root / "lives").exists() and any((self.root / "lives").rglob("*.mp4")))


class ApiTests(unittest.TestCase):
    def test_api_criador_e_bloqueio(self):
        from fastapi.testclient import TestClient

        import app as app_module

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(aut, "DB_PATH", Path(tmp) / "c.db"):
            client = TestClient(app_module.app)
            c = client.post("/api/criadores", json={"nome": "Podcast X", "canais": ["@podcastx"]}).json()
            self.assertEqual(c["canais"], ["@podcastx"])
            r = client.post("/api/start", json={"url": "https://youtu.be/abcdefghijk", "creator_id": c["id"]})
            self.assertEqual(r.status_code, 403)
            r = client.post(f"/api/criadores/{c['id']}/autorizacoes", json={"tipo": "programa", "prova": "link"})
            self.assertEqual(r.status_code, 200)
            lista = client.get("/api/criadores").json()
            self.assertTrue(lista["items"][0]["autorizado"])
            r = client.post(f"/api/autorizacoes/{r.json()['id']}/revogar")
            self.assertFalse(r.json()["valida"])


if __name__ == "__main__":
    unittest.main()
