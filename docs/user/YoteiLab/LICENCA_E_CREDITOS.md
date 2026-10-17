# Licença e créditos

Base AnyPS5: https://github.com/boykopovar/AnyPS5 — copyright de seus respectivos autores, GNU GPL versão 2 somente (GPL-2.0-only). O código recebido pelo usuário está incluído em `base/AnyPS5/`, com as correções documentadas em `docs/Socket-Windows-0.3.3.patch` e `docs/Audit-Guest-0.3.4.patch`, com licença, documentação e atribuições preservadas. Veja `base/source_manifest.json` para identificar o arquivo de origem.

O controlador, menu, testes e documentos do laboratório também são disponibilizados sob GPL-2.0-only, sem garantia. Texto integral: `LICENSE`.

Dependências externas não vieram preenchidas no ZIP recebido. O comando `deps` baixa os submódulos fixados pelo commit indicado no arquivo e preserva suas licenças. Os termos de cada dependência continuam aplicáveis.

Nenhum jogo, biblioteca proprietária de sistema, chave ou firmware acompanha este pacote.

A v0.3.5 integra a proposta de ThatsLeo (AnyPS5, GPL-2.0-only), commit
e893efad2d0a7b90243d07bf6669cc9b5f540432, para a consulta VideoOut com VRR
inativo. Teste de integração adaptado ao snapshot local; alteração completa
em docs/VideoOut-VRR-0.3.5.patch. Origem:
https://github.com/ThatsLeo/AnyPS5/commit/e893efad2d0a7b90243d07bf6669cc9b5f540432

A v0.3.6 acrescenta controles de retenção e diagnóstico locais, sob GPL-2.0-only.
O LRU e os pools já existiam no AnyPS5; as mudanças locais estão em
`docs/Recursos-0.3.6.patch`.

A v0.3.7 adapta o trabalho de Celegans12 na PR #757 (GPL-2.0-only), revisão
`e904eb18c745441e1467e6a80006109c1b54b48d`, com resoluções de integração e
diagnóstico locais. Não corresponde à revisão mais recente da PR.
https://github.com/boykopovar/AnyPS5/pull/757
Patch: `docs/Shaders-0.3.7.patch`.

A v0.3.8 acrescenta o atualizador controlado, backups, restauração e testes
do laboratório sob GPL-2.0-only. A base nativa permanece a da v0.3.7.

## Adaptação da v0.3.9

Suporte experimental a tabelas de imagens por ponteiros de 64 bits, de
Celegans12, PR #758, commit `1ff81c2416f60fbde84465f6bdd1ee3f0f0ab27e`,
aplicado sobre a adaptação da PR #757 já incluída. Crédito e origem preservados.

https://github.com/boykopovar/AnyPS5/pull/758

Ajustes locais: mensagem completa de diagnóstico e instalador da correção
com backup, verificações e retorno. GPL-2.0-only.

## Integrações da base v0.1.2

As revisões abaixo foram incorporadas ao laboratório com seus créditos de origem.
O estado das PRs foi consultado em 15/10/2026; as adaptações locais não transferem
a autoria do trabalho original.

- Celegans12, PRs [757](https://github.com/boykopovar/AnyPS5/pull/757) e
  [758](https://github.com/boykopovar/AnyPS5/pull/758), revisões atualizadas
  `d9e47d1e982a09ae3b013e5898fbb7175c6d9191` e
  `34525fdf7a90411bb874e064c6ab9c981436afcc`: ambas incorporadas pelo upstream.
- ThatsLeo, [PR 5181](https://github.com/boykopovar/AnyPS5/pull/5181),
  `8ddd1f92e4b4beec152022009fe397f38dddf007`: incorporada pelo upstream.
- EagleBearerYS, [PR 5815](https://github.com/boykopovar/AnyPS5/pull/5815),
  `6d17a1dea759ca8dc457e5b1085767c00b24691e`: aberta na consulta.
- ee6aba54, [PR 5484](https://github.com/boykopovar/AnyPS5/pull/5484),
  `67973cd89a957a0e39b8751a1e684414e5a8c041`: incorporada pelo upstream.

O ajuste de `3rdparty/libusb-cmake/libusb/build-aux/gen-describe.ps1` pertence
à árvore da dependência e deve ser revisado separadamente, preservando sua licença.

## Integracoes validadas na RTX 4090 em 16/10/2026

- MarcZX100, [PR #6670](https://github.com/boykopovar/AnyPS5/pull/6670), revisao `4864a748022df4fb673202a0c1f430dac339ed10`: inicializacao do lock LDS por uma unica invocacao.
- Pipodi, [PR #6492](https://github.com/boykopovar/AnyPS5/pull/6492), revisao `349420af7fb2ed7c738d0eefb54d0d4744172026`: descarte de caches de descriptor sets que retem texturas liberadas.

Adaptacoes, invalidacao de cache de shader e testes locais preservam as autorias e GPL-2.0-only. A PR #5353 foi testada separadamente e nao foi incorporada. A validacao de carregamento nao confirma menu, gameplay ou ganho de FPS.
