# UX-012 — Odoo Enterprise

Stato: contratto e decisione, costruito dopo la 2026.1.5478 (non ancora rilasciato). Origine: il confronto con il
pannello di riferimento del 22/11/2026 (`confronto-pannello-22-11.md`, punto 1): là l'account Enterprise si configura
una volta nelle impostazioni, ogni istanza ha l'interruttore «Enterprise Edition» e il badge «Enterprise».
**Provato solo con un repository finto** (`gitstandin@172.30.0.13:enterprise.git` su lab3, §6): il vero
`odoo/enterprise` non è stato provato.

## 1. Le decisioni

1. **Una sorgente per macchina** (Impostazioni › Odoo Enterprise, `/enterprise`, solo gli amministratori della
   macchina): l'indirizzo git (`git@github.com:odoo/enterprise.git` di default) e come lo si raggiunge.
   - `git@…` o `ssh://…`: una **chiave SSH di OmniaCLI fatta apposta** (`enterprise/id_ed25519_enterprise`), di cui la
     pagina mostra la metà pubblica, da aggiungere alle chiavi SSH di un account GitHub che legge `odoo/enterprise`.
     Non è la chiave git dei workspace (`id_ed25519_git`): con quella un admin di workspace che aggiunge un repository
     ssh leggerebbe tutto quello che legge l'account del partner. Le deploy key di GitHub le aggiunge solo chi
     amministra il repository (Odoo SA): per questo è la chiave di un account, e la pagina consiglia un account suo
     che legge soltanto.
   - `https://…`: utente e token, in un file `0600` passato a git con `credential.helper=store --file=…`: mai
     nell'indirizzo, sulla riga di comando o nel log. Le «Git credentials» non servono qui: sono per server e per
     workspace, mentre Enterprise è della macchina.
   - «Prova» fa `git ls-remote --heads` con quello che dice il modulo e dice i branch di versione (`NN.0`).
2. **Il codice sulla macchina**: una copia per branch di versione, solo l'ultimo commit (`--depth 1`). L'utente di
   OmniaCLI la scarica in `/var/lib/omniacli/enterprise/checkouts/<versione>`; l'helper di root (`enterprise-sync`) la
   legge **con `tar` eseguito come l'utente di OmniaCLI** e la scrive, senza `.git`, in
   `/var/lib/omniacli-enterprise/<versione>` (root, `755`/`644`, fuori da ogni workspace; il filtro `data` di
   `tarfile` rifiuta link che escono, device, bit set-id), file per file sul posto: la cartella che le istanze montano
   resta la stessa. Si scarica la prima volta che serve, con «Aggiorna» della pagina, e con l'Aggiorna dell'istanza
   quando lo preme un amministratore della macchina.
3. **Per istanza** (scheda Impostazioni › Odoo Enterprise): l'interruttore c'è solo per un'istanza fatta da un
   template (Docker Compose) e lo usa **solo un amministratore della macchina**, qui o da un altro pannello (una
   persona, `a_machine(me)` falso). Un admin di workspace vede lo stato e il badge, non l'interruttore: il codice si
   raggiunge con l'accesso del partner. Acceso: il `compose.override.yaml` di OmniaCLI monta la copia su
   `/mnt/enterprise:ro`, `odoo.conf` la mette **prima** nell'`addons_path`, `instance.json` tiene `{series, service}`,
   e Odoo si riavvia (un job). **Non si installa niente**: il database resta Community finché non ci si installa
   `web_enterprise`, con il pulsante separato «Installa web_enterprise» o dalle Applicazioni di Odoo; il codice di
   abbonamento è del cliente, e lo chiede Odoo. Non abbiamo visto il pannello di riferimento installare moduli da
   solo, quindi non lo facciamo.
4. **Spegnere** è rifiutato, prima del clic e di nuovo nel job, finché nel database di produzione o di test è
   installato un modulo della copia (`installed`, `to upgrade`, `to remove`), con i nomi: Odoo non partirebbe senza il
   loro codice. Se il database non risponde, anche: si avvia, poi si spegne. Niente nome da scrivere: il rifiuto basta.
5. **Una staging di una produzione Enterprise è Enterprise** dal primo avvio (`create` con `_enterprise`), anche fatta
   su un'altra macchina: quella la prende se ha la copia o la sorgente, altrimenti il job dice di impostarla. Un
   OmniaCLI più vecchio dall'altra parte ignora il parametro (la staging nasce Community): non provato.
6. **Le istanze che c'erano già** (tipo di riavvio `process`/`systemd`): si legge il loro `addons_path`; se una cartella
   contiene `web_enterprise`, badge «Enterprise» e la frase «il suo addons_path ha già Odoo Enterprise». Nessun
   interruttore: il loro codice non è di OmniaCLI.
7. **Il badge** «Enterprise» sta nella testata dell'istanza e nell'elenco Istanze (anche di ogni macchina collegata),
   ed è cercabile («enterprise»). Un utente di workspace lo vede; non vede cartelle né chiavi.

## 2. Visibilità

| Cosa | admin macchina (qui o da un altro pannello) | admin/dev/utente di workspace | un'altra macchina da sola |
|---|---|---|---|
| Impostazioni › Odoo Enterprise | sì | no (403) | no |
| Interruttore, Installa web_enterprise | sì | no: lo stato e la frase «Lo attiva un amministratore della macchina» | no |
| Badge, stato on/off | sì | sì | — |
| Cartella della copia, chiave, token | la cartella e la chiave pubblica; il token mai | mai | mai |

## 3. Pulsanti (EN / IT, `title` diverso dal testo)

- Pagina: «Make the key» / «Crea la chiave», «Test» / «Prova», **«Save» / «Salva» (il primario)**, «Update» /
  «Aggiorna» per riga, «Fetch» / «Scarica» dopo una Prova.
- Istanza: «Switch on…» / «Attiva…», «Switch off…» / «Spegni…» (pericolo), «Install web_enterprise» /
  «Installa web_enterprise». Nessun primario: la scheda ha già «Salva e riavvia». Conferme con `omniaAsk`, rifiuti con
  `omniaSay`.

## 4. I limiti (scritti, non risolti)

- **Una copia condivisa**: un Aggiorna della copia cambia il codice sul disco di tutte le istanze Enterprise della
  versione su questa macchina, di ogni workspace; lo prendono al loro prossimo riavvio, e i moduli cambiati li aggiorna
  l'Aggiorna di ciascuna. Per questo l'Aggiorna di un'istanza scarica Enterprise solo se lo preme un amministratore
  della macchina. Nessuna copia per istanza, nessun commit fissato per istanza.
- L'Aggiorna di un'istanza fa il backup prima solo se cambiano i suoi repository, non se cambia solo Enterprise.
- La chiave dell'account GitHub legge tutto quello che legge quell'account (`StrictHostKeyChecking=accept-new`: la
  chiave di github.com si fida la prima volta).
- Una copia non usata non si cancella dalla pagina (a mano: `/var/lib/omniacli-enterprise/<versione>`).
- L'aggiornamento automatico pianificato non scarica Enterprise.

## 5. Dove sta nel codice

`omniacli/enterprise.py` (sorgente, opzioni git, stato delle copie), `workspacehelper.enterprise_sync`,
`workspacehelper.enterprise_switch`, `managedreplica.override_text` (la parte Enterprise convive con quella della
replica, e ogni riscrittura dell'override la tiene), `stacktemplates.conf_text(odoo_enterprise=…)`, le route
`/enterprise`, `/api/enterprise/*`, `/api/servers/{server}/enterprise`,
`/api/servers/{server}/{role}/enterprise/install` in `app.py`; test in `tests/test_enterprise.py`.

## 6. La prova (22/11/2026, lab3 e lab4, interfaccia in italiano)

Con un repository finto, non con `odoo/enterprise`: `gitstandin@172.30.0.13:enterprise.git`, branch `18.0` e `19.0`,
due moduli vuoti (`web_enterprise` e `web_enterprise_standin`, «stand-in … not Odoo's code»).

- lab3, Impostazioni › Odoo Enterprise: «Crea la chiave», chiave aggiunta all'utente del repository finto, «Prova»
  → «✓ 2 branch; versioni: 18.0, 19.0», «Salva».
- Istanza usa-e-getta `acme-ent` (odoo-19): «Attiva…» → job riuscito, la 19.0 scaricata dal job stesso (2 moduli),
  badge «Enterprise»; nelle Applicazioni di Odoo i due moduli finti si possono attivare; «Installa web_enterprise» →
  installato; «Spegni…» rifiutato con il nome del modulo; disinstallato in Odoo, «Spegni…» → job riuscito,
  `addons_path` di nuovo senza `/mnt/enterprise`.
- Staging di `acme-ent` acceso: nasce Enterprise (`addons paths: [… '/mnt/enterprise' …]` nel log di Odoo).
- «Aggiorna» della 19.0 dopo un commit nuovo: 1 file aggiunto, 1 cambiato, stessa cartella (stesso inode), il
  container lo legge senza riavvio; dal container la cartella è in sola lettura.
- lab4 attraverso il pannello di lab3 (`/links/lab4/…`): sorgente, chiave, Prova, e «Attiva…» su `acme-ent4` → job
  riuscito; l'elenco Istanze di lab3 mostra il badge delle istanze delle due macchine.
- Un admin del workspace: badge e stato sì, nessun pulsante, nessuna cartella o chiave nella pagina; `/enterprise` e
  il POST dell'interruttore → 403.
- 0 dialoghi del browser, 0 errori di pagina; le sole risposte ≥ 400 sono i rifiuti voluti (409, 403).
