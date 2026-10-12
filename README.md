# downow

Cole o link de um vídeo do YouTube e baixe o áudio em **MP3 (320 kbps)** ou **M4A (sem conversão)**.
Um arquivo só, com ffmpeg e Deno embutidos. Uma versão para Linux Mint 21/22 e outra para Windows 64 bits.

## Como usar
1. Abra o downow e cole o link (botão **Colar**). A miniatura e o título aparecem para você conferir.
2. Escolha MP3 ou M4A e clique em **Baixar**.
3. Ao terminar aparece **Download completo** e o botão **Abrir pasta**.
4. Em **⚙** você escolhe a pasta de destino (padrão: Música), a aparência e **Atualizar motor**.

Links de playlist: o downow baixa só o vídeo indicado.

## Gerar os executáveis (GitHub Actions)
1. Crie um repositório no GitHub e envie todos os arquivos desta pasta.
2. Aba **Actions** → **Gerar executáveis do downow** → **Run workflow**.
3. Quando terminar, baixe `downow-linux-x86_64` e `downow-windows-x64.exe` em *Artifacts*.
   (Se criar uma tag `v1.0.0`, os dois arquivos também vão para a aba *Releases*.)

Para gerar localmente: `pip install -r requirements.txt pyinstaller` e `python build.py`
(o resultado fica em `dist/`; o `.exe` só pode ser gerado no Windows, o binário Linux só no Linux).

## Instalar no Linux Mint
Deixe na mesma pasta: `downow-linux-x86_64`, `instalar-linux.sh`, `downow.desktop` e a pasta `assets/` (com o `icon.png`).
Rode `./instalar-linux.sh`. O downow passa a aparecer no menu de aplicativos.
Ou simplesmente: `chmod +x downow-linux-x86_64 && ./downow-linux-x86_64`.

## Manutenção
O YouTube muda com frequência. Se um download falhar, use **⚙ › Atualizar motor**: o downow baixa o yt-dlp mais novo
para `~/.local/share/downow/engine` (Linux) ou `%LOCALAPPDATA%\downow\engine` (Windows), sem precisar recompilar.

## Observações
- O YouTube serve áudio em Opus/AAC de ~128–160 kbps. O MP3 em 320 kbps evita perda extra, mas não melhora a fonte.
  O M4A preserva o áudio original.
- O Windows SmartScreen e alguns antivírus costumam alertar sobre executáveis do PyInstaller sem assinatura.
  É falso positivo comum; em "Mais informações › Executar mesmo assim" você segue.
- Use apenas com vídeos seus, livres ou com permissão de quem os publicou.
