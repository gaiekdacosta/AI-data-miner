"""Testes do motor — só biblioteca padrão, para rodarem no CI sem instalar nada.

    python3 -m unittest discover -s tests -v

O que depende de configuração (``perfis.py`` lê o ambiente no import) roda num
subprocesso com o ambiente montado pelo teste: assim um caso não contamina o
outro, e o .env de quem roda os testes não interfere.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import consultar  # noqa: E402
import validar_sql  # noqa: E402
import vetorial  # noqa: E402


def _rodar(codigo, **env):
    """Roda `codigo` num Python novo, na raiz do projeto, só com o ambiente dado.

    Saída padrão parseada como JSON (o código imprime o que quer comparar).
    """
    # MOTOR_ENV=nenhum: o .env de quem roda os testes não entra no subprocesso.
    ambiente = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": RAIZ, "MOTOR_ENV": "nenhum"}
    ambiente.update(env)
    with tempfile.TemporaryDirectory() as vazio:
        r = subprocess.run([sys.executable, "-c", textwrap.dedent(codigo)],
                           cwd=vazio, env=ambiente, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"subprocesso falhou:\n{r.stderr}")
    return json.loads(r.stdout)


class ValidarSelect(unittest.TestCase):
    def test_aceita_select_e_with(self):
        self.assertEqual(consultar._validar_select("SELECT 1;"), "SELECT 1")
        consultar._validar_select("with x as (select 1 as a) select a from x")
        # Coluna com nome parecido com comando não é barrada.
        consultar._validar_select("SELECT created_at, updated_by FROM t")

    def test_recusa_escrita(self):
        for sql in ("DELETE FROM t", "update t set a=1", "DROP TABLE t",
                    "INSERT INTO t VALUES (1)", "PRAGMA query_only = OFF",
                    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
                    "SELECT 1; DROP TABLE t", "", "   ;  "):
            with self.subTest(sql=sql), self.assertRaises(ValueError):
                consultar._validar_select(sql)


class Perfis(unittest.TestCase):
    CODIGO = """
        import json, perfis as P
        print(json.dumps({"perfis": P.PERFIS, "padrao": P.PERFIL_PADRAO,
                          "sensiveis": P.SENSIVEIS, "iniciais": P.INICIAIS,
                          "bancos": {p: P.banco(p) for p in P.PERFIS},
                          "engines": {p: P.engine(p) for p in P.PERFIS}}))
    """

    def test_registro(self):
        r = _rodar(self.CODIGO, PERFIS="vendas, rh  BI_vendas 9ruim", PERFIS_SENSIVEIS="rh",
                   db_vendas_engine="postgresql", db_rh_engine="mssql",
                   db_bi_vendas_engine="mariadb", db_bi_vendas_banco="vendas")
        self.assertEqual(r["perfis"], ["vendas", "rh", "bi_vendas"])   # "9ruim" é inválido
        self.assertEqual(r["padrao"], "vendas")
        self.assertEqual(r["iniciais"], ["vendas", "bi_vendas"])       # sem o sensível
        self.assertEqual(r["bancos"]["bi_vendas"], "vendas")
        self.assertEqual(r["engines"], {"vendas": "postgres", "rh": "sqlserver",
                                        "bi_vendas": "mysql"})

    def test_iniciais_vazio_e_nenhum(self):
        base = dict(PERFIS="a,b", PERFIS_SENSIVEIS="b", db_a_engine="sqlite", db_b_engine="sqlite")
        self.assertEqual(_rodar(self.CODIGO, PERFIS_INICIAIS="", **base)["iniciais"], ["a"])
        self.assertEqual(_rodar(self.CODIGO, PERFIS_INICIAIS="nenhum", **base)["iniciais"], [])
        self.assertEqual(_rodar(self.CODIGO, PERFIS_INICIAIS="b", **base)["iniciais"], ["b"])

    def test_padrao_invalido_cai_no_primeiro(self):
        r = _rodar(self.CODIGO, PERFIS="a,b", PERFIL_PADRAO="zzz",
                   db_a_engine="sqlite", db_b_engine="sqlite")
        self.assertEqual(r["padrao"], "a")

    def test_engine_ausente_e_erro_claro(self):
        codigo = """
            import json, perfis as P
            try:
                P.engine("x"); print(json.dumps("passou"))
            except RuntimeError as e:
                print(json.dumps(str(e)))
        """
        self.assertIn("db_x_engine ausente", _rodar(codigo, PERFIS="x"))


class Guardrail(unittest.TestCase):
    CATALOGO = {"PEDIDOS": {"ID", "STATUS", "DATA_PEDIDO"}, "ITENS": {"ID", "PEDIDO_ID"}}

    def test_coluna_inexistente_com_sugestao(self):
        problemas = validar_sql.validar_schema(
            "SELECT p.stauts FROM pedidos p", self.CATALOGO, perfil=None)
        self.assertEqual(len(problemas), 1)
        self.assertIn("talvez: STATUS", problemas[0])

    def test_alias_cte_e_literal_nao_confundem(self):
        sql = """WITH base AS (SELECT p.id FROM pedidos p WHERE p.status = 'x.y')
                 SELECT b.id FROM base b -- i.coluna_que_nao_existe"""
        self.assertEqual(validar_sql.validar_schema(sql, self.CATALOGO, perfil=None), [])

    def test_catalogo_de_sistema_qualificado_nao_e_acusado(self):
        # information_schema.tables não é a tabela TABLES do usuário. Sem perfil
        # configurado o engine é desconhecido, então o teste vale pelo prefixo.
        sql = "SELECT p.id FROM pedidos p JOIN information_schema.tables t ON 1 = 1"
        for engine in ("postgres", "mysql", "sqlserver"):
            with self.subTest(engine=engine):
                self.assertTrue(validar_sql._eh_tabela_sistema("INFORMATION_SCHEMA.TABLES", engine))
        _, _, _, qualificadas = validar_sql._extrair(validar_sql._limpar_sql(sql))
        self.assertEqual(qualificadas, {"TABLES": {"INFORMATION_SCHEMA.TABLES"}})

    def test_sem_catalogo_libera(self):
        self.assertEqual(validar_sql.validar_schema("SELECT x.y FROM z x", {}, None), [])


class Lexico(unittest.TestCase):
    def test_radical_e_prefixo(self):
        self.assertEqual(vetorial._desflexionar("devolucoes"), "devolucao")
        self.assertEqual(vetorial._desflexionar("ordens"), "ordem")
        termos = vetorial.termos("Devoluções do faturamento")
        self.assertIn("devolucao", termos)
        self.assertIn("fatur", termos)          # prefixo de 5 letras
        self.assertNotIn("do", termos)          # stopword


class SqlitePontaAPonta(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.db = os.path.join(cls.dir, "loja.db")
        spec = importlib.util.spec_from_file_location(
            "criar_banco", os.path.join(RAIZ, "exemplo", "criar_banco.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.gerar(cls.db)
        cls.env = dict(PERFIS="loja", db_loja_engine="sqlite", db_loja_database=cls.db)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_consulta_e_numeros_das_notas(self):
        # Os números citados em NOTES/loja/ têm de continuar verdadeiros: o banco
        # de exemplo é determinístico e este teste é o que garante isso.
        r = _rodar("""
            import json, consultar
            _, linhas, _ = consultar._executar(
                "SELECT status, COUNT(*) FROM pedidos GROUP BY status ORDER BY status")
            _, bruto, _ = consultar._executar(
                "SELECT ROUND(SUM(faturamento_bruto), 2) FROM vw_faturamento_mensal")
            print(json.dumps({"status": linhas, "bruto": bruto[0][0]}))
        """, **self.env)
        self.assertEqual(r["status"], [["aberto", 46], ["cancelado", 182], ["faturado", 1972]])
        self.assertEqual(r["bruto"], 2317595.29)

    def test_escrita_barrada_pelo_proprio_banco(self):
        # Por baixo do validador: o arquivo abre em modo ro.
        r = _rodar("""
            import json, consultar
            try:
                consultar._executar("DELETE FROM pedidos"); print(json.dumps("escreveu"))
            except Exception as e:
                print(json.dumps(str(e)))
        """, **self.env)
        self.assertIn("readonly", r)

    def test_teto_de_tempo_cancela(self):
        # Consulta propositalmente infinita: o vigia tem de cortá-la no teto.
        r = _rodar("""
            import json, consultar
            sql = ("WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) "
                   "SELECT COUNT(*) FROM n")
            try:
                consultar._executar(sql); print(json.dumps("terminou"))
            except TimeoutError as e:
                print(json.dumps("timeout"))
        """, db_loja_query_timeout_s="1", **self.env)
        self.assertEqual(r, "timeout")


@unittest.skipUnless(importlib.util.find_spec("httpx"), "httpx não instalado (pip install -r requirements.txt)")
class PainelTokens(unittest.TestCase):
    def test_criar_rotacionar_renovar_revogar(self):
        with tempfile.TemporaryDirectory() as d:
            tokens = os.path.join(d, "tokens.caddy")
            shutil.copy(os.path.join(RAIZ, "caddy", "tokens.caddy.example"), tokens)
            r = _rodar("""
                import json, admin_tokens as T
                T.recarregar = lambda: (True, "")          # sem Caddy no teste
                antes = len(T.listar())
                label, token = T.criar("Maria Silva", ["vendas"])
                p = T.listar()[0]
                _, novo = T.rotacionar(label)
                _, data = T.renovar(label, dias=10)
                _, perfis = T.definir_perfis(label, ["rh", "vendas", "inexistente"])
                depois = T.listar()[0]
                try:
                    T.criar("maria silva")
                    duplicou = True
                except T.ErroDeToken:
                    duplicou = False
                T.revogar(label)
                print(json.dumps({"antes": antes, "label": label, "perfis_criado": p.perfis,
                                  "token_mudou": novo != token, "expira": depois.expira == data,
                                  "perfis": perfis, "duplicou": duplicou,
                                  "fim": len(T.listar())}))
            """, PERFIS="vendas,rh", PERFIS_SENSIVEIS="rh", TOKENS_PATH=tokens,
               TOKENS_AUDIT_DIR=os.path.join(d, "aud"),
               db_vendas_engine="sqlite", db_rh_engine="sqlite")
        self.assertEqual(r["antes"], 0)                 # o exemplo vem todo comentado
        self.assertEqual(r["label"], "maria_silva")
        self.assertEqual(r["perfis_criado"], ["vendas"])
        self.assertTrue(r["token_mudou"])
        self.assertTrue(r["expira"])
        self.assertEqual(r["perfis"], ["vendas", "rh"])  # ordem canônica, sem o inexistente
        self.assertFalse(r["duplicou"])
        self.assertEqual(r["fim"], 0)


if __name__ == "__main__":
    unittest.main()
