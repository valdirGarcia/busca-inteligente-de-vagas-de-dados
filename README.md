# Busca Inteligente de Vagas de Dados

App local em Python/Streamlit para buscar vagas publicas na area de dados, salvar em SQLite e ranquear por compatibilidade com um perfil profissional configurado em YAML.

## O que o app faz

1. Busca vagas em fontes publicas como Greenhouse, Lever, Ashby, SmartRecruiters, Gupy, Remotive, RemoteOK, Arbeitnow e Solides.
2. Normaliza cargo, empresa, local, descricao, fonte e data de publicacao.
3. Calcula um score de match com base em cargo, senioridade, skills, dominios de negocio, localizacao e termos de penalizacao.
4. Exibe recomendacoes em ordem decrescente de match.
5. Permite salvar vagas, marcar candidaturas, ignorar vagas e adicionar vagas manualmente.
6. Mantem um dashboard local com funil de recomendacao, status, fontes e empresas.
7. Remove vagas antigas/irrelevantes do banco, preservando candidaturas e vagas manuais.
8. Filtra recomendacoes por modalidade/local: remoto, presencial na regiao, hibrido na regiao e fora da regiao.
9. Evita gravar no SQLite vagas abaixo do match minimo configurado.
10. Permite filtrar recomendacoes por fonte de vagas.
11. Mostra badges de local/modalidade e uma explicacao numerica do match por componente.
12. Permite ativar/desativar fontes na aba de configuracoes.
13. Registra motivo ao ignorar uma vaga e consolida esses motivos no dashboard.
14. Exporta candidaturas em CSV.
15. Mantem historico das buscas executadas e do volume coletado/elegivel por fonte.
16. Mantem a busca e o banco focados em vagas publicadas nos ultimos 7 dias.

## Como rodar

Instale as dependencias:

```bash
pip install -r requirements.txt
```

Crie seu perfil local a partir do exemplo:

```bash
copy data\profile.example.yaml data\profile.yaml
```

Edite `data/profile.yaml` com seu perfil e rode:

```bash
python -m streamlit run streamlit_app.py
```

No Windows, tambem da para usar:

```bash
run_app.bat
```

## Arquivos locais que nao sobem para o Git

Por privacidade, estes arquivos ficam apenas na maquina local:

- `data/profile.yaml`
- `data/app.db`
- `curriculo/`
- `PROJECT_CONTEXT.md`
- `.env` e segredos do Streamlit

Use `data/profile.example.yaml` como template publico.

## CLI opcional

Para testar a busca pelo terminal:

```bash
python -m app.search --profile data/profile.yaml --sources data/sources.yaml --limit 20
```

## Fontes

- Greenhouse: ATS por empresa.
- Lever: ATS por empresa.
- Ashby: ATS por empresa com job board publico.
- SmartRecruiters: ATS por empresa com postings publicos.
- Gupy: portal publico brasileiro, filtrado por termos de dados.
- Remotive: vagas remotas por categoria.
- RemoteOK: vagas remotas/tech.
- Arbeitnow: job board publico.
- Solides: portal publico brasileiro, usando links estaveis do dominio `vagas.solides.com.br`.

As fontes ativas ficam em `data/sources.yaml`.

## Observacoes

Vagas do LinkedIn e Indeed nao aparecem automaticamente, a menos que tambem estejam em uma fonte publica coletada pelo app. Para esses casos, use a aba `Adicionar vaga`. A API oficial da Gupy exige token para uso empresarial, mas o app usa o endpoint publico do portal de vagas.
