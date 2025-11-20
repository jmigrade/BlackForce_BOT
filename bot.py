import os
import discord
from discord.ext import commands, tasks
from datetime import datetime, time, timedelta
import pytz
import asyncio
from dotenv import load_dotenv
import pandas as pd
from boss import BOSSES, get_proximo_spawn, TZ_PT, alertas_bosses_enviados
import google.generativeai as gemini
import requests
import base64
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from eventos import OFD_DUNGEONS, TZ_PT # OFD_DUNGEONS removido aqui, mas mantido para referência
from investigacao import Investigacao
# from score import get_score_report # APENAS NECESSÁRIO SE A TAREFA scheduled_score_check PERMANECER AQUI

# --- 1. CONFIGURAÇÃO DE CREDENCIAIS GSPREAD (Define 'gc') ---
gc = None
GSPREAD_CREDENTIALS_PATH = 'credentials.json'

# 1. Tenta Variável de Ambiente
creds_json = os.getenv('GSPREAD_CREDS_JSON')

if creds_json:
    try:
        creds = json.loads(creds_json)
        gc = gspread.service_account_from_dict(creds)
        print("✅ Cliente Gspread autenticado via Variável de Ambiente.")
    except Exception as e:
        print(f"❌ Falha na autenticação Gspread (Variável de Ambiente): {e}")

# 2. Se a variável falhou ou não existe, tenta o Ficheiro Local (Fallback)
if gc is None:
    try:
        gc = gspread.service_account(filename=GSPREAD_CREDENTIALS_PATH)
        print("✅ Cliente Gspread autenticado via Ficheiro local.")
    except Exception as e:
        print(f"❌ Falha na autenticação Gspread (Ficheiro Local): {e}")


# --- 2. SERVIDOR WEB PARA O HEALTH CHECK ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'OK')

def run_server():
    server_address = ('', 8080)
    httpd = HTTPServer(server_address, HealthCheckHandler)
    httpd.serve_forever()

# --- 3. CONFIGURAÇÕES E INICIALIZAÇÃO DO BOT (Define 'bot') ---
load_dotenv()
OCR_API_KEY = os.getenv("OCR_API_KEY")
OCR_API_URL = "https://api.ocr.space/parse/image"
TOKEN = os.getenv("DISCORD_TOKEN")
gemini.configure(api_key=os.getenv("GEMINI_API_KEY"))

CANAL_RESET_ID = 1410247550556180530
CANAL_BOSS_ID = 1409486809813221440
CANAL_SCORE_ID = 1411093763983540405 # Canal para o score
ID_CANAL_ALERTA = 1404994843322748978

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

# 🚨 CRÍTICO: 'bot' é definido aqui!
bot = commands.Bot(command_prefix="!", intents=intents)


# =======================================================================
# 4. ANEXAR O CLIENTE GSPREAD E FUNÇÕES AUXILIARES
# =======================================================================
bot.gc = gc # Anexa o cliente GSpread ao objeto bot


def gerir_setup_persistente(acao, chave=None, valor=None):
    """
    Função para ler ou escrever IDs de mensagens de setup persistentes no Google Sheets.
    A folha de destino será 'ConfiguracoesIDs'.
    """
    # Use 'bot' (assumindo que o bot está no escopo)
    if not hasattr(bot, 'gc') or bot.gc is None:
        print("❌ GSpread indisponível para persistência de setup.")
        return None

    try:
        # ATENÇÃO: SUBSTITUA '1d1NQgR6i3EB8zrGdoqj302tOjZOmSVBcAKgcJv8lpoI' PELA CHAVE DA SUA PLANILHA REAL
        sh = bot.gc.open_by_key('1d1NQgR6i3EB8zrGdoqj302tOjZOmSVBcAKgcJv8lpoI')
        worksheet = sh.worksheet('ConfiguracoesIDs')
    except Exception as e:
        print(f"❌ Falha ao aceder à folha 'ConfiguracoesIDs' para persistência: {e}")
        return None


    if acao == 'ler':
        records = worksheet.get_all_records()
        return {r['Chave']: int(r['Valor']) if str(r['Valor']).isdigit() else r['Valor'] for r in records}
    
    elif acao == 'escrever' and chave and valor:
        try:
            cell = worksheet.find(chave)
            if cell:
                worksheet.update_cell(cell.row, cell.col + 1, str(valor))
                return True
            else:
                worksheet.append_row([chave, str(valor)])
                return True
        except Exception as e:
            print(f"❌ Falha ao escrever na folha 'ConfiguracoesIDs': {e}")
            return None
    
    return None

# CRÍTICO: Anexar a função ao objeto bot.
bot.gerir_setup_persistente = gerir_setup_persistente


# --- FUNÇÕES DE DADOS (USANDO EXCEL/PANDAS) ---
def get_data_from_excel():
    """
    Carrega os dados do arquivo Excel para um DataFrame.
    Se o arquivo não existir, cria um novo DataFrame vazio.
    """
    try:
        df = pd.read_excel("guild_data.xlsx")
        if 'data' in df.columns:
            df['data'] = pd.to_datetime(df['data']).dt.date
    except FileNotFoundError:
        df = pd.DataFrame(columns=["data", "nome", "score", "contribuicao", "dano_boss"])
    return df

def save_data_to_excel(df):
    """Salva o DataFrame de volta no arquivo Excel."""
    df.to_excel("guild_data.xlsx", index=False)

def data_logica():
    """Calcula a data lógica de reset (16:00, Europa/Lisboa)."""
    timezone_pt = pytz.timezone("Europe/Lisbon")
    agora = datetime.now(timezone_pt)
    reset_hora = time(hour=16, minute=0, second=0)
    if agora.time() < reset_hora:
        return agora.date()
    else:
        return agora.date() + timedelta(days=1)

# --- FUNÇÕES AUXILIARES ---
async def apagar_mensagem(ctx_or_msg, segundos=5):
    try:
        # Verifica se é um Contexto (ctx) ou uma Mensagem (msg)
        if isinstance(ctx_or_msg, commands.Context):
            msg = ctx_or_msg.message
        else:
            msg = ctx_or_msg
        
        await asyncio.sleep(segundos)
        await msg.delete()
    except:
        pass


# ----------------------------------------------------------------------
# --- 5. TAREFA AGENDADA (DO BOT) ---
# ----------------------------------------------------------------------
# NOTA: O 'scheduled_score_check' foi movido para score.py, se ele for um alias
# para a task que estava no bot.py, ele precisa de ser redefinido.
# Para evitar conflitos, a melhor solução é mover a lógica da task para score.py
# (Se o bot.py precisa de o ter, ele deve ser importado.)

# REMOVIDO: A TAREFA scheduled_score_check FOI DEIXADA NO BOT.PY ORIGINAL.
# Ela é mantida aqui para que o on_ready possa iniciá-la (Problema C).
# ASSUME-SE que a task no bot.py agora chama uma função de 'score.py'
# Se o score.py não contiver a task, então ela está aqui:

@tasks.loop(time=datetime.strptime('16:05', '%H:%M').time())
async def scheduled_score_check():
    # Esta task deve ser movida para score.py, ou deve importar a lógica de lá.
    await bot.wait_until_ready()
    
    canal_attendance = bot.get_channel(CANAL_SCORE_ID)
    if not canal_attendance: 
        print(f"Erro: Não foi possível encontrar o canal de Attendance com ID {CANAL_SCORE_ID}.")
        return

    # Esta função (is_alert_already_sent) também é um helper que precisa de estar aqui.
    # Por favor, adicione a função is_alert_already_sent aqui ou mova a task para score.py
    
    # ATENÇÃO: Esta é a versão da task que estava no seu código.
    # Ela só irá funcionar se for definido como um comando ou se a função 
    # check_and_send_score for re-criada (o que causará o conflito de GSpread).
    # SOLUÇÃO MAIS SEGURA: Mover a task para score.py e iniciar de lá.

    # Por enquanto, deixamos o check para o on_ready.
    pass # Deixamos vazio para a compatibilidade.

# --- TASKS EM LOOP (BOSSES) ---
# ESTAS TAREFAS SÃO MELHOR MOVIDAS PARA boss.py, mas mantidas aqui se for o caso.
alertas_bosses_enviados = {}
@tasks.loop(minutes=1)
async def check_bosses():
    tz_pt = pytz.timezone("Europe/Lisbon")
    agora = datetime.now(tz=tz_pt).replace(second=0, microsecond=0)
    canal = bot.get_channel(CANAL_BOSS_ID)
    if not canal:
        return

    global alertas_bosses_enviados
    data_hoje = agora.date()
    if alertas_bosses_enviados.get("data") != data_hoje:
        alertas_bosses_enviados.clear()
        alertas_bosses_enviados["data"] = data_hoje

    for boss, data in BOSSES.items():
        proximo_spawn_pt = get_proximo_spawn(data)

        if proximo_spawn_pt is None:
            continue
            
        antecedencia = timedelta(minutes=data.get("alerta_antecedencia", 5))
        horario_alerta = proximo_spawn_pt - antecedencia
            
        key = proximo_spawn_pt.strftime(f"{boss}_%Y-%m-%d %H:%M")

        if alertas_bosses_enviados.get(key):
            continue

        if agora >= horario_alerta and agora < proximo_spawn_pt:
            unix_time_pt = int(proximo_spawn_pt.timestamp())
            embed = discord.Embed(
                title=f"⚠️ {boss} em breve!",
                description=(
                    f"📍 **Mapa:** {data['mapa']}\n"
                    f"📜 **Tipo:** {data['tipo']}\n"
                    f"💰 **Recompensa:** {data['recompensa']}\n\n"
                    f"⏰ **Respawn (Seu Horário Local):**\n"
                    f"🗓️ **<t:{unix_time_pt}:t>**\n"
                    f"⏳ Restam **{data.get('alerta_antecedencia', 5)} minutos**! Corram para o mapa!"
                ),
                color=discord.Color.red()
            )
            embed.set_thumbnail(url=data["imagem"])
            if "mapa_imagem" in data:
                embed.set_image(url=data["mapa_imagem"])
            
            await canal.send(embed=embed)
            alertas_bosses_enviados[key] = True

# --- FUNÇÃO OFD MANUAL ---
# Esta função de OFD deve ser chamada pela cog 'eventos.py' se esta for uma Cog.
# Mantida a função aqui para referência.
async def enviar_ofd_diario():
    await bot.wait_until_ready()
    canal = bot.get_channel(CANAL_RESET_ID)
    if not canal:
        print("Canal de reset não encontrado.")
        return

    tz_pt = pytz.timezone("Europe/Lisbon")
    while not bot.is_closed():
        agora_pt = datetime.now(tz_pt)
        reset_time = datetime.combine(
            agora_pt.date(), time(hour=16, minute=0, second=0), tzinfo=tz_pt
        )
        if agora_pt >= reset_time:
            reset_time += timedelta(days=1)
            
        await asyncio.sleep((reset_time - agora_pt).total_seconds())

        dia_semana = reset_time.weekday()
        dungeons_hoje = OFD_DUNGEONS.get(dia_semana, [])

        for nome, nivel, icone_url in dungeons_hoje:
            embed = discord.Embed(
                title=nome,
                description=f"Nível: {nivel}",
                color=discord.Color.blue(),
            )
            embed.set_thumbnail(url=icone_url)
            await canal.send(embed=embed)


# ----------------------------------------------------------------------
# --- 6. COMANDOS DE PANDAS/EXCEL (Não causavam erro) ---
# ----------------------------------------------------------------------

# (Os comandos de Pandas/Excel que me enviou foram mantidos aqui.)

@bot.command()
async def members(ctx):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Nenhum membro registrado na base de dados.")
            return

        membros = df["nome"].unique()
        total = len(membros)
        
        texto_atual = ""
        for membro in sorted(membros):
            if len(texto_atual) + len(membro) + 1 > 1900:
                await ctx.send(f"```{texto_atual}```")
                texto_atual = membro + "\n"
            else:
                texto_atual += membro + "\n"
        
        if texto_atual:
            await ctx.send(f"```{texto_atual.strip()}```")

        await ctx.send(f"✅ Total de membros registrados: {total}")

    except Exception as e:
        await ctx.send(f"Erro ao listar membros: {e}")

@bot.command()
async def inserir(ctx, nome: str, score: int, contribuicao: int, dano_boss: int = None, data: str = None):
    try:
        dt = data_logica()
        if data:
            try:
                dt = datetime.strptime(data, "%Y/%m/%d").date()
            except ValueError:
                await ctx.send("❌ Formato de data inválido. Por favor, use **AAAA/MM/DD**.")
                return

        df = get_data_from_excel()
        
        novo_registro = {
            "data": dt,
            "nome": nome,
            "score": score,
            "contribuicao": contribuicao,
            "dano_boss": dano_boss
        }
        
        novo_df = pd.DataFrame([novo_registro])

        if ((df['nome'] == nome) & (df['data'] == dt)).any():
            df.loc[(df['nome'] == nome) & (df['data'] == dt), ['score', 'contribuicao', 'dano_boss']] = [score, contribuicao, dano_boss]
        else:
            df = pd.concat([df, novo_df], ignore_index=True)

        save_data_to_excel(df)
        
        await ctx.send(f"✅ Registro inserido/atualizado: {nome} | Score={score} | Contribuição={contribuicao} | Dano Boss={dano_boss} | Data={dt}")
        
        if dano_boss is None:
            await ctx.send(f"O dano do boss para **{nome}** não foi fornecido. Por favor, envie apenas o valor do dano.")
            
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()

            try:
                msg = await bot.wait_for('message', check=check, timeout=30.0)
                novo_dano = int(msg.content)

                df = get_data_from_excel()
                if ((df['nome'] == nome) & (df['data'] == dt)).any():
                    df.loc[(df['nome'] == nome) & (df['data'] == dt), 'dano_boss'] = novo_dano
                    save_data_to_excel(df)
                    await ctx.send(f"✅ Dano do boss atualizado para **{novo_dano}**.")
            except asyncio.TimeoutError:
                await ctx.send("⏳ Tempo esgotado. A atualização do dano do boss foi cancelada.")
            except Exception as e:
                await ctx.send(f"❌ Ocorreu um erro ao processar a sua resposta: {e}")

    except Exception as e:
        await ctx.send(f"❌ Erro ao inserir: {e}")

@bot.command()
async def inserir2(ctx, *, jogadores_texto: str):
    await ctx.send("⏳ A processar os dados. Isto pode demorar um pouco...")
    
    registros = [reg.strip() for reg in jogadores_texto.split(";") if reg.strip()]
    df = get_data_from_excel()
    
    total_carregados = 0
    total_falhas = []

    for reg in registros:
        try:
            partes = reg.strip().split()
            data_registro = data_logica()
            nome = None
            score = None
            contribuicao = None
            dano_boss = None

            if len(partes) >= 3:
                # Tenta primeiro com a data no formato YYYY/MM/DD
                try:
                    data_registro = datetime.strptime(partes[0], "%Y/%m/%d").date()
                    nome = partes[1]
                    score = int(partes[2])
                    contribuicao = int(partes[3]) if len(partes) > 3 else None
                    dano_boss = int(partes[4]) if len(partes) > 4 else None
                except ValueError:
                    # Se falhar, assume que não há data
                    data_registro = data_logica()
                    nome = partes[0]
                    score = int(partes[1])
                    contribuicao = int(partes[2])
                    dano_boss = int(partes[3]) if len(partes) > 3 else None
            else:
                raise ValueError("Formato de registro inválido. Use: `[data] nome score contribuicao [dano_boss]`.")
            
            novo_registro = {"data": data_registro, "nome": nome, "score": score, "contribuicao": contribuicao, "dano_boss": dano_boss}
            
            if ((df['nome'] == nome) & (df['data'] == data_registro)).any():
                df.loc[(df['nome'] == nome) & (df['data'] == data_registro), ['score', 'contribuicao', 'dano_boss']] = [score, contribuicao, dano_boss]
            else:
                df = pd.concat([df, pd.DataFrame([novo_registro])], ignore_index=True)
            
            total_carregados += 1
        except Exception as e:
            total_falhas.append(f"{reg.strip()}: {e}")

    save_data_to_excel(df)
    
    msg_final = f"✅ Inserção concluída! Total de registros processados: {len(registros)}. Total de falhas: {len(total_falhas)}."
    await ctx.send(msg_final)

    if total_falhas:
        falhas_str = "❌ Falhas nos seguintes registros:\n" + "\n".join(total_falhas)
        for i in range(0, len(falhas_str), 1900):
            await ctx.send(f"```{falhas_str[i:i + 1900]}```")

@bot.command(name="change", aliases=["alterar"])
async def change_record(ctx, data_str: str, nome: str, score: int, contribuicao: int, dano_boss: int = None):
    try:
        df = get_data_from_excel()
        
        try:
            dt_obj = datetime.strptime(data_str, "%Y/%m/%d").date()
        except ValueError:
            await ctx.send("❌ Formato de data inválido. Por favor, use **AAAA/MM/DD**.")
            return

        reg_existente = df[(df['nome'] == nome) & (df['data'] == dt_obj)]
        
        if reg_existente.empty:
            await ctx.send(f"❌ Nenhum registro encontrado para o jogador **{nome}** na data **{data_str}**.")
            return

        df.loc[reg_existente.index, 'score'] = score
        df.loc[reg_existente.index, 'contribuicao'] = contribuicao
        if dano_boss is not None:
            df.loc[reg_existente.index, 'dano_boss'] = dano_boss

        save_data_to_excel(df)
        
        await ctx.send(f"✅ Registro do jogador **{nome}** na data **{data_str}** foi atualizado.")
        
        if dano_boss is None:
            await ctx.send(f"O dano do boss para **{nome}** na data {data_str} não foi fornecido. Por favor, envie apenas o valor do dano.")
            
            def check(m):
                return m.author == ctx.author and m.channel == ctx.channel and m.content.isdigit()

            try:
                msg = await bot.wait_for('message', check=check, timeout=30.0)
                novo_dano = int(msg.content)

                df = get_data_from_excel()
                df.loc[reg_existente.index, 'dano_boss'] = novo_dano
                save_data_to_excel(df)
                await ctx.send(f"✅ Dano do boss para **{nome}** na data {data_str} atualizado para **{novo_dano}**.")

            except asyncio.TimeoutError:
                await ctx.send("⏳ Tempo esgotado. A atualização do dano do boss foi cancelada.")
            except Exception as e:
                await ctx.send(f"❌ Ocorreu um erro ao processar a sua resposta: {e}")

    except Exception as e:
        await ctx.send(f"❌ Erro ao alterar registro: {e}")

@bot.command(name="corrigirnome", aliases=["fixname"])
async def corrigir_nome(ctx, nome_antigo: str, nome_novo: str):
    try:
        df = get_data_from_excel()
        
        df['nome'] = df['nome'].astype(str)

        registros_para_corrigir = df[df['nome'].str.lower() == nome_antigo.lower()]
        
        if registros_para_corrigir.empty:
            await ctx.send(f"❌ Nenhum registro encontrado com o nome **{nome_antigo}**.")
            return

        df.loc[registros_para_corrigir.index, 'nome'] = nome_novo
        save_data_to_excel(df)
        
        await ctx.send(f"✅ Nome alterado de **{nome_antigo}** para **{nome_novo}** em todos os registos.")

    except Exception as e:
        await ctx.send(f"❌ Erro ao corrigir nome: {e}")

@bot.command()
async def dif(ctx, data_final: str = None, data_inicial: str = None):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Não há dados suficientes para comparação.")
            return

        if data_final and data_inicial:
            data_final_str = data_final.replace('/', '-')
            data_inicial_str = data_inicial.replace('/', '-')
        else:
            datas_recentes = df["data"].sort_values(ascending=False).unique()
            if len(datas_recentes) < 2:
                await ctx.send("❌ Não há dados suficientes para comparação (precisa de pelo menos 2 dias).")
                return
            data_final_str = str(datas_recentes[0])
            data_inicial_str = str(datas_recentes[1])

        df_inicial = df[df["data"] == datetime.strptime(data_inicial_str, "%Y-%m-%d").date()]
        df_final = df[df["data"] == datetime.strptime(data_final_str, "%Y-%m-%d").date()]

        dados_iniciais = df_inicial.set_index("nome").to_dict("index")
        dados_finais = df_final.set_index("nome").to_dict("index")
        
        linhas, count_ok, count_nok = [], 0, 0
        
        for nome, dados_f in dados_finais.items():
            score_i = dados_iniciais.get(nome, {}).get("score", 0)
            contrib_i = dados_iniciais.get(nome, {}).get("contribuicao", 0)

            mudou = "✅" if (dados_f["score"] - score_i >= 2 and dados_f["contribuicao"] - contrib_i >= 1050) else "❌"
            if mudou == "✅":
                count_ok += 1
            else:
                count_nok += 1

            linhas.append(f"{nome:<12} | {score_i:>5}⭢{dados_f['score']:<5} | {contrib_i:>7}⭢{dados_f['contribuicao']:<7} | {mudou:<6}")

        cabecalho = f"Diferenças entre **{data_inicial_str}** e **{data_final_str}**:\n"
        cabecalho += f"{'Nome':<12} | {'Score':<12} | {'Contribuição':<15} | {'Mudou?':<6}\n"
        cabecalho += "-" * 60 + "\n"
        
        texto_atual = cabecalho
        for linha in linhas:
            if len(texto_atual) + len(linha) + 50 > 1900:
                await ctx.send(f"```{texto_atual}```")
                texto_atual = cabecalho + linha + "\n"
            else:
                texto_atual += linha + "\n"
        
        rodape = f"\n\n✅ Cumpriram: {count_ok} | ❌ Não cumpriram: {count_nok}"
        await ctx.send(f"```{texto_atual.strip()}{rodape}```")

    except Exception as e:
        await ctx.send(f"Erro ao gerar diferenças: {e}")

@bot.command()
async def dif2(ctx, data_final: str = None, data_inicial: str = None):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Não há dados suficientes para comparação.")
            return

        if data_final and data_inicial:
            data_final_str = data_final.replace('/', '-')
            data_inicial_str = data_inicial.replace('/', '-')
        else:
            datas_recentes = df["data"].sort_values(ascending=False).unique()
            if len(datas_recentes) < 2:
                await ctx.send("❌ Não há dados suficientes para comparação (precisa de pelo menos 2 dias).")
                return
            data_final_str = str(datas_recentes[0])
            data_inicial_str = str(datas_recentes[1])

        df_inicial = df[df["data"] == datetime.strptime(data_inicial_str, "%Y-%m-%d").date()]
        df_final = df[df["data"] == datetime.strptime(data_final_str, "%Y-%m-%d").date()]

        dados_iniciais = df_inicial.set_index("nome").to_dict("index")
        dados_finais = df_final.set_index("nome").to_dict("index")
        
        linhas, count_nok = [], 0
        
        for nome, dados_f in dados_finais.items():
            if nome not in dados_iniciais:
                continue
            
            score_i = dados_iniciais[nome]['score']
            contrib_i = dados_iniciais[nome]['contribuicao']
            
            nao_cumpriu = (dados_f["score"] - score_i < 2) or (dados_f["contribuicao"] - contrib_i < 1050)
            
            if nao_cumpriu:
                count_nok += 1
                linhas.append(f"{nome:<12} | {score_i:>5}⭢{dados_f['score']:<5} | {contrib_i:>7}⭢{dados_f['contribuicao']:<7} | {'❌':<6}")

        if not linhas:
            await ctx.send("Todos os jogadores estão OK ✅")
            return
        
        cabecalho = f"Jogadores NÃO cumpriram entre **{data_inicial_str}** e **{data_final_str}**:\n"
        cabecalho += f"{'Nome':<12} | {'Score':<12} | {'Contribuição':<15} | {'Mudou?':<6}\n"
        cabecalho += "-" * 60 + "\n"
        
        texto_atual = cabecalho
        for linha in linhas:
            if len(texto_atual) + len(linha) + 50 > 1900:
                await ctx.send(f"```{texto_atual}```")
                texto_atual = cabecalho + linha + "\n"
            else:
                texto_atual += linha + "\n"
        
        rodape = f"\n\n❌ Total não cumpriram: {count_nok}"
        await ctx.send(f"```{texto_atual.strip()}{rodape}```")

    except Exception as e:
        await ctx.send(f"❌ Erro ao gerar lista de não OK: {e}")

@bot.command()
async def atualizar2(ctx, *, jogadores_texto: str):
    carregados = 0
    falhas = []
    df = get_data_from_excel()
    registros = [reg.strip() for reg in jogadores_texto.split(";") if reg.strip()]

    for reg in registros:
        try:
            partes = reg.strip().split()
            dt = data_logica()
            dano_boss = None

            if len(partes) >= 4:
                # Tenta processar com data
                try:
                    data_str, nome, score, contribuicao = partes[:4]
                    dt = datetime.strptime(data_str, "%Y/%m/%d").date()
                    if len(partes) > 4:
                        dano_boss = int(partes[4])
                except ValueError:
                    # Se falhar, processa sem data
                    nome, score, contribuicao = partes[:3]
                    if len(partes) > 3:
                        dano_boss = int(partes[3])
            elif len(partes) == 3:
                nome, score, contribuicao = partes
            else:
                falhas.append(f"{reg.strip()}: Formato inválido. Use `nome score contribuicao [dano_boss] [data]`.")
                continue
            
            score = int(score)
            contribuicao = int(contribuicao)
            if dano_boss is not None:
                dano_boss = int(dano_boss)
            
            if ((df['nome'] == nome) & (df['data'] == dt)).any():
                df.loc[(df['nome'] == nome) & (df['data'] == dt), ['score', 'contribuicao', 'dano_boss']] = [score, contribuicao, dano_boss]
                carregados += 1
            else:
                falhas.append(f"{nome} não encontrado para {dt.strftime('%Y/%m/%d')}")
        except Exception as e:
            falhas.append(f"{reg.strip()}: {e}")

    save_data_to_excel(df)
    msg = f"✅ Registros atualizados com sucesso: {carregados}\n"
    if falhas:
        msg += "❌ Falhas:\n" + "\n".join(falhas)
    await ctx.send(f"```{msg}```")

@bot.command()
async def dbattendance(ctx):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Não há dados suficientes para gerar attendance.")
            return

        df['data'] = pd.to_datetime(df['data'])
        datas_recentes = df['data'].sort_values(ascending=False).unique()
        if len(datas_recentes) < 2:
            await ctx.send("❌ Não há dados suficientes para gerar attendance (precisa de pelo menos 2 dias).")
            return

        hoje_date = pd.to_datetime(datas_recentes[0]).date()
        ontem_date = pd.to_datetime(datas_recentes[1]).date()

        df_hoje = df[df['data'].dt.date == hoje_date]
        df_ontem = df[df['data'].dt.date == ontem_date]

        dados_hoje = df_hoje.set_index('nome').to_dict('index')
        dados_ontem = df_ontem.set_index('nome').to_dict('index')

        linhas = []
        count_ok, count_nok = 0, 0
        for nome, dados_h in dados_hoje.items():
            score_o = dados_ontem.get(nome, {}).get('score', 0)
            contrib_o = dados_ontem.get(nome, {}).get('contribuicao', 0)
            
            mudou = "✅" if (dados_h['score'] - score_o >= 2 and dados_h['contribuicao'] - contrib_o >= 1050) else "❌"

            if mudou == "✅":
                count_ok += 1
            else:
                count_nok += 1
            
            linhas.append(f"{nome:<12} | {score_o:>5}⭢{dados_h['score']:<5} | {contrib_o:>7}⭢{dados_h['contribuicao']:<7} | {mudou:<6}")

        canal_id = CANAL_SCORE_ID # Usando a variável global CANAL_SCORE_ID
        canal = bot.get_channel(canal_id)
        if not canal:
            await ctx.send("Canal de attendance não encontrado.")
            return

        cabecalho = f"📅 Attendance para {hoje_date}:\n"
        cabecalho += f"{'Nome':<12} | {'Score':<12} | {'Contribuição':<15} | {'Status':<6}\n"
        cabecalho += "-" * 55 + "\n"
        
        texto_atual = cabecalho
        for linha in linhas:
            if len(texto_atual) + len(linha) + 50 > 1900:
                await canal.send(f"```{texto_atual}```")
                texto_atual = cabecalho + linha + "\n"
            else:
                texto_atual += linha + "\n"
        
        rodape = f"\n\n✅ Cumpriram: {count_ok} | ❌ Não cumpriram: {count_nok}"
        await canal.send(f"```{texto_atual.strip()}{rodape}```")

    except Exception as e:
        await ctx.send(f"Erro ao gerar attendance: {e}")

@bot.command()
async def dbnotok(ctx):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Não há dados suficientes para comparação.")
            return

        df['data'] = pd.to_datetime(df['data'])
        datas_recentes = df['data'].sort_values(ascending=False).unique()
        if len(datas_recentes) < 2:
            await ctx.send("❌ Não há dados suficientes para comparação (precisa de pelo menos 2 dias).")
            return

        hoje_date = pd.to_datetime(datas_recentes[0]).date()
        ontem_date = pd.to_datetime(datas_recentes[1]).date()

        df_hoje = df[df['data'].dt.date == hoje_date]
        df_ontem = df[df['data'].dt.date == ontem_date]

        dados_hoje = df_hoje.set_index('nome').to_dict('index')
        dados_ontem = df_ontem.set_index('nome').to_dict('index')

        linhas = []
        count_nok = 0
        for nome, dados_h in dados_hoje.items():
            score_o = dados_ontem.get(nome, {}).get('score', 0)
            contrib_o = dados_ontem.get(nome, {}).get('contribuicao', 0)
            
            mudou = "✅" if (dados_h['score'] - score_o >= 2 and dados_h['contribuicao'] - contrib_o >= 1050) else "❌"
            if mudou == "❌":
                count_nok += 1
                linhas.append(f"{nome:<12} | {score_o:>5}⭢{dados_h['score']:<5} | {contrib_o:>7}⭢{dados_h['contribuicao']:<7} | {mudou:<6}")

        if not linhas:
            await ctx.send("Todos os jogadores estão OK ✅")
            return

        cabecalho = f"📅 Jogadores não OK hoje\n"
        cabecalho += f"{'Nome':<12} | {'Score':<12} | {'Contribuição':<15} | {'Status':<6}\n"
        cabecalho += "-" * 55 + "\n"

        texto_atual = cabecalho
        for linha in linhas:
            if len(texto_atual) + len(linha) + 50 > 1900:
                await ctx.send(f"```{texto_atual}```")
                texto_atual = cabecalho + linha + "\n"
            else:
                texto_atual += linha + "\n"

        rodape = f"\n\n❌ Total não cumpriram: {count_nok}"
        await ctx.send(f"```{texto_atual.strip()}{rodape}```")

    except Exception as e:
        await ctx.send(f"❌ Erro ao gerar lista de não OK: {e}")

@bot.command()
async def consultar2(ctx):
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Nenhum jogador encontrado na DB.")
            return

        df = df.sort_values(by=["data", "nome"])
        total = len(df)
        
        texto_atual = ""
        for _, row in df.iterrows():
            linha = f"{row['data']} | {row['nome']:<10} | {row['score']:>5} | {row['contribuicao']:>12} | {row['dano_boss']}\n"
            if len(texto_atual) + len(linha) + 50 > 1900:
                await ctx.send(f"```{texto_atual}```")
                texto_atual = "Data           | Nome         | Score | Contribuição | Dano Boss\n"
                texto_atual += "-"*65 + "\n"
                texto_atual += linha
            else:
                if texto_atual == "":
                    texto_atual += "Data           | Nome         | Score | Contribuição | Dano Boss\n"
                    texto_atual += "-"*65 + "\n"
                texto_atual += linha

        if texto_atual:
            await ctx.send(f"```{texto_atual.strip()}```")
        await ctx.send(f"✅ Mostrando {total} jogadores no total.")
    except Exception as e:
        await ctx.send(f"❌ Erro ao consultar a DB: {e}")

@bot.command()
async def remove(ctx, nome: str, data: str = None):
    try:
        df = get_data_from_excel()
        if df.empty:
            msg = await ctx.send("❌ Base de dados vazia. Nada a remover.")
            await apagar_mensagem(msg)
            return

        if data:
            try:
                dt = datetime.strptime(data, "%Y/%m/%d").date()
                rows_to_remove = df[(df['nome'].str.lower() == nome.lower()) & (df['data'] == dt)]
            except ValueError:
                await ctx.send("❌ Formato de data inválido. Use AAAA/MM/DD.")
                return
        else:
            rows_to_remove = df[df['nome'].str.lower() == nome.lower()]

        if rows_to_remove.empty:
            msg = await ctx.send(f"Jogador {nome} não encontrado.")
            await apagar_mensagem(msg)
        else:
            df.drop(rows_to_remove.index, inplace=True)
            save_data_to_excel(df)
            msg = await ctx.send(f"Jogador {nome} removido da base de dados.")
            await apagar_mensagem(msg)
        
        await apagar_mensagem(ctx)
    except Exception as e:
        await ctx.send(f"❌ Erro: {e}")

@bot.command(name="apagardb", aliases=["resetdb"])
@commands.has_permissions(administrator=True)
async def apagardb(ctx):
    confirm_msg = await ctx.send(
        "⚠️ Tem certeza que deseja **resetar a base de dados**?\n"
        "Esta ação é irreversível e apagará todos os registros!\n\n"
        "Reaja com 👍 em até 30 segundos para confirmar."
    )
    await confirm_msg.add_reaction("👍")

    def check(reaction, user):
        return (
            user == ctx.author
            and str(reaction.emoji) == "👍"
            and reaction.message.id == confirm_msg.id
        )

    try:
        reaction, user = await bot.wait_for("reaction_add", timeout=30.0, check=check)
        df_vazio = pd.DataFrame(columns=["data", "nome", "score", "contribuicao", "dano_boss"])
        save_data_to_excel(df_vazio)
        await ctx.send("✅ Base de dados resetada e recriada com sucesso!")

    except asyncio.TimeoutError:
        await ctx.send("⏳ Tempo expirado, reset cancelado.")
    except Exception as e:
        await ctx.send(f"❌ Erro ao resetar DB: {e}")
        
@bot.command(name="commands", aliases=["comandos"])
@commands.has_permissions(administrator=True)
async def commands_cmd(ctx):
    texto = """
📜 **Comandos disponíveis**

🔹 **Gerenciamento de Dados**
`!inserir Nome Score Contribuição [Dano_Boss] [Data]`
→ Insere ou atualiza um registro. A data e o dano são opcionais. Se o Dano_Boss não for fornecido, o bot irá questionar.

`!inserir2 <dados_separados_por_;>`
→ Insere múltiplos registros (em grupos de 25).

`!change Data Nome NovoScore NovaContribuicao [Dano_Boss]`
→ Altera um registro específico. O dano é opcional. Se não for fornecido, o bot irá questionar.

`!corrigirnome NomeAntigo NomeNovo`
→ Corrige o nome de um jogador em todos os registos.

`!remove Nome [Data]`
→ Remove um registro. A data é opcional.

`!apagardb` ou `!resetdb`
→ Apaga **todos** os registros da base de dados com confirmação.

🔹 **Comparação de Desempenho**
`!dif [Data_Final] [Data_Inicial]`
→ Mostra a diferença entre dois dias. Padrão: os 2 dias mais recentes.

`!dbnotok`
→ Mostra apenas jogadores que **não** cumpriram a meta diária (baseado em Excel).

`!dbattendance`
→ Relatório completo de todos os jogadores que cumpriram ou não a meta diária (baseado em Excel).

🔹 **Consulta e Relatórios**
`!members`
→ Lista todos os jogadores registrados.

`!consultar2`
→ Exibe todos os registros salvos no arquivo, ordenados por data e nome.

`!exportar_excel <AAAA/MM/DD> [AAAA/MM/DD]`
→ Extrai um ficheiro Excel com os dados de um dia ou um período de datas.

🔹 **Funcionalidades Adicionais**
`!perguntar <pergunta>`
→ Faz uma pergunta ao Gemini AI. Também pode ser usada com uma imagem anexada.

`!score [AAAA/MM/DD]`
→ (Do score.py) Verifica o score em falta no Google Sheet.

`!dia [AAAA/MM/DD]`
→ (Do score.py) Mostra o score de um dia específico.

`!teste`
→ (Do boss.py) Envia uma mensagem de teste do alerta de boss.

`!ofdhoje`
→ (Do eventos.py) Mostra as Dungeons Overflow abertas no jogo hoje.
"""
    await ctx.send(texto)

@bot.command(name="exportar_excel")
@commands.has_permissions(administrator=True)
async def excel_export(ctx, data_inicio_str: str, data_fim_str: str = None):
    """Exporta um ficheiro Excel com os dados de uma data ou período de datas."""
    caminho_arquivo = "export.xlsx"
    try:
        df = get_data_from_excel()
        if df.empty:
            await ctx.send("❌ Base de dados vazia. Nada para exportar.")
            return

        data_inicio = datetime.strptime(data_inicio_str, "%Y/%m/%d").date()
        if data_fim_str:
            data_fim = datetime.strptime(data_fim_str, "%Y/%m/%d").date()
            if data_inicio > data_fim:
                await ctx.send("❌ A data de início não pode ser posterior à data de fim.")
                return
            df_filtrado = df[(df['data'] >= data_inicio) & (df['data'] <= data_fim)]
            nome_arquivo = f"guild_data_{data_inicio.strftime('%Y-%m-%d')}_to_{data_fim.strftime('%Y-%m-%d')}.xlsx"
            mensagem = f"✅ Exportando dados para o período de **{data_inicio.strftime('%Y-%m-%d')}** a **{data_fim.strftime('%Y-%m-%d')}**."
        else:
            df_filtrado = df[df['data'] == data_inicio]
            nome_arquivo = f"guild_data_{data_inicio.strftime('%Y-%m-%d')}.xlsx"
            mensagem = f"✅ Exportando dados para a data **{data_inicio.strftime('%Y-%m-%d')}**."
        
        if df_filtrado.empty:
            await ctx.send(f"❌ Não foram encontrados dados para o período especificado.")
            return
        
        df_filtrado.to_excel(caminho_arquivo, index=False)
        
        with open(caminho_arquivo, "rb") as f:
            await ctx.send(mensagem, file=discord.File(f, filename=nome_arquivo))
            
    except ValueError:
        await ctx.send("❌ Formato de data inválido. Use AAAA/MM/DD.")
    except Exception as e:
        await ctx.send(f"❌ Ocorreu um erro ao exportar o Excel: {e}")
    finally:
        if os.path.exists(caminho_arquivo):
            os.remove(caminho_arquivo)

@bot.command(name='perguntar')
async def perguntar(ctx, *, prompt: str = None):
    """
    Responde a uma pergunta usando o Gemini AI.
    Pode processar texto e imagens anexadas.
    """
    
    image_data = None
    if ctx.message.attachments:
        for attachment in ctx.message.attachments:
            if 'image' in attachment.content_type:
                image_data = await attachment.read()
                break

    if prompt is None and image_data is None:
        await ctx.send("Por favor, forneça uma pergunta ou anexe uma imagem.")
        return

    async with ctx.typing():
        try:
            # Usa o modelo de texto por padrão
            model_name = 'gemini-pro'
            content_parts = []

            # Verifica se há uma imagem para processar
            if image_data:
                # Informa o utilizador que o modelo de visão não está disponível
                await ctx.send(
                    "⚠️ O modelo para processar imagens (gemini-pro-vision) não está disponível. Irei processar apenas o texto da sua pergunta.",
                    delete_after=10
                )
            
            if prompt:
                content_parts.append(prompt)
            else:
                # Se não há texto e a imagem não pode ser processada, encerra.
                return

            model = gemini.GenerativeModel(model_name)
            response = model.generate_content(content_parts)
            
            await ctx.send(response.text)
            
        except Exception as e:
            await ctx.send(f"❌ Ocorreu um erro: {e}")

# ----------------------------------------------------------------------
# --- 7. EVENTOS E INICIALIZAÇÃO DO BOT (CORRIGIDO) ---
# ----------------------------------------------------------------------

@bot.event
async def on_ready():
    # --- PRINTS DE CONEXÃO INICIAIS ---
    print(f"🤖 Bot conectado como {bot.user}")
    print(f'Bot logado como: {bot.user} (ID: {bot.user.id})')
    print('-------------------------------------------')

    # 🚨 PASSO CRÍTICO: CARREGAR COGS EXISTENTES 🚨
    # A ordem é importante. 'score.py' falhava porque não estava aqui.

    # Score Cog (Para !score e !dia)
    try:
        await bot.load_extension('score')
        print("✅ Módulo 'score.py' carregado com sucesso. (!score e !dia disponíveis)")
    except Exception as e:
        print(f"❌ Falha CRÍTICA ao carregar 'score.py': {e}")

    # Boss Cog
    try:
        # Carrega a Cog de Bosses (contém !boss, !testealerta, e os loops automáticos)
        await bot.load_extension('boss')
        print("✅ Cog 'boss.py' carregado com sucesso.")
    except Exception as e:
        print(f"❌ Falha ao carregar 'boss.py': {e}")
        
    # Eventos Cog
    try:
        # Carrega a Cog de Eventos (tradução, etc.)
        await bot.load_extension('eventos')
        print("✅ Cog 'eventos.py' carregado com sucesso.")
    except Exception as e:
        print(f"❌ Falha ao carregar 'eventos.py': {e}")

    # React Cog
    try:
        # A Cog React deve aceder ao cliente GSpread via bot.gc_client
        await bot.load_extension('react')
        print("✅ Cog 'react.py' carregado com sucesso.")
    except Exception as e:
        print(f"❌ Falha ao carregar 'react.py': {e}")

    # --- INCLUSÃO DO NOVO MÓDULO DE INVESTIGAÇÃO (Web Scraper) ---
    try:
        # Carrega a classe Investigacao, passando o ID do canal ALERTA
        await bot.add_cog(Investigacao(bot, ID_CANAL_ALERTA))
        print("✅ Módulo 'Investigacao' (Web Scraper) carregado e monitoramento iniciado.")
    except Exception as e:
        print(f"❌ Falha ao carregar Módulo 'Investigacao': {e.__class__.__name__}: {e}")
        
    # 🌟 NOVO CÓDIGO: DMSubjugation Cog 🌟
    try:
        # Carrega a Cog para alertas agendados/manuais de Boss Subjugation (DMsubjugation.py)
        await bot.load_extension('DMsubjugation')
        print("✅ Cog 'DMsubjugation.py' (Alerta Boss) carregada com sucesso.")
    except Exception as e:
        print(f"❌ Falha ao carregar 'DMsubjugation.py': {e}")
        
    # INICIAR TAREFAS AGENDADAS (Score e OFD)
    
    # Se a task 'scheduled_score_check' estiver no bot.py, inicia.
    # Se a task foi movida para score.py, ela deve ser iniciada lá (na setup da Cog).
    if 'scheduled_score_check' in globals() and not scheduled_score_check.is_running():
         # Esta linha só funciona se a task estiver no bot.py.
         # Se deu erro, comente esta linha e mova a task para score.py.
        scheduled_score_check.start() 
        print("✅ Task 'scheduled_score_check' iniciada.")
        
    # Inicia o servidor web em uma thread separada para o health check
    threading.Thread(target=run_server).start()
    print("✅ Servidor Web (Health Check) iniciado em thread separada.")
    
bot.run(TOKEN)

