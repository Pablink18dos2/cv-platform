# START HERE — Arrancar un proyecto AI-native desde cero

Guía paso a paso basada en el Módulo 1 del AI Dev Tools Zoomcamp y en el artículo
[AI-Native Development: Specifications, Loop and Graph Engineering](https://alexeyondata.substack.com/p/ai-native-development-specifications).

**La idea en una frase:** el cuello de botella ya no es escribir código, es *decir con
precisión qué quieres* y *comprobar lo que te devuelven*. Un agente débil que te malinterpreta
escribe 50 líneas rotas. Un agente potente que te malinterpreta crea 8 ficheros, los conecta
entre sí y añade tests que pasan. Funciona, pero no es lo que necesitabas.

---

## Cómo usar esta plantilla

```bash
# 1. Copia la carpeta con el nombre de tu proyecto
cp -r _starter-template ../mi-proyecto     # PowerShell: Copy-Item -Recurse
cd ../mi-proyecto

# 2. Inicializa git ANTES de que ningún agente toque nada
git init
git add .
git commit -m "Bootstrap from AI-native starter template"
```

Los ficheros vienen con marcadores `<...>` y bloques `TODO`. **No los rellenes a mano de golpe**:
cada fase de abajo te dice cuál toca y cuándo.

> ⚠️ Borra este `START-HERE.md` (o muévelo a `_docs/`) antes de publicar el repo del proyecto.
> Es tu guía, no documentación del producto.

---

## Las 4 capas de "engineering"

Ten esto en la cabeza; ordena todo lo demás:

| Capa | Qué controla | Dónde vive en esta plantilla |
|---|---|---|
| **Prompt engineering** | Lo que dices en *un* mensaje | `prompts/prompts.md` |
| **Context engineering** | Lo que el agente sabe *antes de empezar* la sesión | `AGENTS.md`, `_docs/` |
| **Loop engineering** | *Cuándo para* de trabajar | `/goal` + condición de parada |
| **Graph engineering** | *Quién hace qué* cuando hay varios agentes | `_docs/team/`, `_docs/process.md` |

---

# FASE 0 — De la idea vaga a la especificación

⏱️ 20–40 min · 🚫 **Todavía no abras el agente de código**

Este es el paso que casi todo el mundo se salta y el que más caro sale saltarse.

Abre un **chat assistant** (ChatGPT, Claude.ai — no el agente de tu IDE) y pega:

```text
I want to build <tu idea en una frase>.

Help me set the scope for this project precisely. I want to brainstorm with you
and understand how the tool should work. Give me options.

Ask me one question at a time and keep your output short.
```

> 💡 **Consejo:** el "una pregunta cada vez" es lo que hace que funcione. Si le pides que
> te dé el spec entero de golpe, rellenará los huecos con suposiciones — que es exactamente
> lo que intentas evitar. Alexey hace esta sesión **por dictado**, hablando; sale más
> natural y más completo que escribiendo.

Responde hasta que hayáis cubierto, como mínimo:

- ¿Quién usa esto? (roles concretos, no "usuarios")
- ¿Qué problema resuelve para cada rol?
- ¿Cuál es el flujo principal, paso a paso?
- ¿Qué **no** hace? (esto vale tanto como lo que sí hace)
- ¿Qué es un MVP aquí? ¿Qué es v2?

Cuando estés satisfecho:

```text
Save everything to a markdown file that I can download.
```

Guarda ese fichero en **`_docs/plan.md`** (sobrescribe la plantilla que viene aquí) y commitea:

```bash
git add _docs/plan.md
git commit -m "Add project plan"
```

✅ **Hecho cuando:** alguien que no ha hablado contigo puede leer `_docs/plan.md` y
explicarte qué construyes y para quién.

---

# FASE 1 — Elegir stack y arquitectura

⏱️ 10 min · Ya sí: abre el agente de código en la carpeta del proyecto.

```text
Read _docs/plan.md. Propose multiple options for the tech stack and
explain each option.

Don't write code yet.
```

> 💡 **Consejo:** elige un stack que **puedas revisar**, no el "mejor". Si no sabes leer Go,
> no elijas Go: no podrás detectar cuándo el agente se equivoca, y detectar eso es
> literalmente tu trabajo ahora. Alexey elige Django por eso mismo.
> Si no tienes preferencia, también es válido dejar que elija el agente.

Anota la decisión y el porqué en `_docs/plan.md`. Commit.

---

# FASE 2 — Backlog

⏱️ 20 min

```text
Create a backlog with tasks in _docs/tasks.md.

Each task should be small enough to finish in one session, and
independent enough that I could hand it to someone who has not read
the others.

Use this template for each task:

## <number>. <title>
Goal: <one line>
Description: <two or three sentences on what the task involves>

The first task should be setting up an empty project with a passing test.

Don't write code yet.
```

**Ahora revisa tú.** Es tu trabajo, no el suyo:

- Une las tareas demasiado pequeñas.
- Parte las que no caben en una sesión.
- **Borra todo lo que no sea MVP.** Sé agresivo aquí.

Cuando te guste el backlog, pásalo a GitHub issues (necesitas `gh` CLI autenticado):

```text
Create a public GitHub repo for this project.
Move each task from _docs/tasks.md into a GitHub issue.
```

> 💡 A partir de aquí **los issues de GitHub son el backlog canónico**. `_docs/tasks.md` ya
> no se usa — bórralo o déjalo como registro histórico, pero no lo mantengas en paralelo.
> Dos backlogs = ningún backlog.

---

# FASE 3 — Context engineering

⏱️ 15 min · Los ficheros ya están en la plantilla, solo hay que rellenarlos.

Rellena **`AGENTS.md`** con los comandos reales de tu stack (`TODO` dentro).

`CLAUDE.md` ya contiene `@AGENTS.md` — no lo toques. Sirve para que Claude Code lea el
mismo fichero que Codex/OpenCode y tu flujo no dependa de una herramienta.

> 💡 **Consejo — mantén `AGENTS.md` CORTO.** Es lo único que se carga en *todas* las
> sesiones. Todo lo demás va a `_docs/` y se enlaza desde `AGENTS.md`, para que el agente
> cargue el design system solo en tareas de UI y las guías de testing solo en tareas de tests.
> Un `AGENTS.md` de 300 líneas es contexto desperdiciado en cada sesión.

> 💡 **Consejo — son documentos vivos.** Cuando corrijas al agente en mitad de una sesión,
> no te limites a corregirlo. Ejecuta esto y ya no tendrás que repetirlo nunca más:
>
> ```text
> Based on the corrections I made, find the relevant documents
> and update them.
>
> Commit the current work before changing the documents.
> ```

---

# FASE 4 — La primera tarea

```text
Implement task 1.
```

Debería crear el esqueleto del proyecto, las dependencias y un test que pasa.
Verifica que el test pasa **tú, en tu terminal**. Commit.

---

# FASE 5 — Grooming: el agente Product Manager

Los issues del backlog aún no son lo bastante precisos. Si le das un issue vago al ingeniero,
volvemos al problema del principio: rellenará los huecos.

**Groomear** = hacer el issue tan específico que un ingeniero pueda implementarlo sin
preguntar nada.

El rol ya está en `_docs/team/pm.md` y la plantilla de issue en `_docs/task-template.md`.

```text
Groom issue #4
```

Revisa el resultado. Un issue groomeado tiene 4 secciones: **Goal**, **Acceptance criteria**
(comprobables mirando la pantalla), **Out of scope**, **Constraints**.

> 💡 **Este es el momento más barato para cazar un malentendido.** En el grooming el issue
> es un párrafo y corregirlo cuesta una frase. Después de implementarlo, cuesta una reescritura.

---

# FASE 6 — Loop engineering

Para groomear el resto, la tentación es:

```text
Groom all GitHub issues. Process one issue at a time.
```

Funciona… hasta que el agente para y pregunta *"he groomeado 1, 2 y 3, ¿sigo?"*. La respuesta
siempre es sí, pero te ha bloqueado esperándola.

En vez de eso, dale un **objetivo**:

```text
/goal groom all issues
```

El harness (Claude Code, Codex) comprueba si la condición se cumple cuando el agente para, y
lo reanuda si no. Es un `while` alrededor del agente.

> ⚠️ **La condición de parada tiene que ser comprobable por el modelo.**
> ✅ "all issues are groomed", "all tests pass", "no file is over 200 lines"
> ❌ "make the code better" → el agente para demasiado pronto o no para nunca.

---

# FASE 7 — Implementación: el agente Software Engineer

Rol en `_docs/team/software-engineer.md`. **Sesión nueva** (contexto limpio):

```text
Implement issue #2
```

Reglas que lleva incorporadas: implementa contra los acceptance criteria sin cambiarlos,
se queda dentro de los ficheros que el issue nombra, escribe tests, commitea a menudo y
**no cierra el issue**.

---

# FASE 8 — QA: el agente QA Engineer

> Cuando el mismo agente escribe el código y lo juzga, se está corrigiendo su propio examen.
> Si le preguntas "¿esto está bien?", la respuesta será que sí.

Rol en `_docs/team/qa-engineer.md`. **Sesión nueva otra vez**:

```text
Test issue #2
```

Devuelve un veredicto `PASS` / `FAIL` como comentario en el issue, con cada acceptance
criterion evaluado y el comando de tests que ejecutó. **No arregla nada** — solo reporta.

Si es `FAIL`: sesión nueva de ingeniero, pásale el comentario de QA, itera hasta `PASS`.

---

# FASE 9 — Graph engineering: el orquestador

Cuando los tres roles funcionan sueltos, automatiza el grafo:
`PM → Engineer → QA → (FAIL: vuelve a Engineer / PASS: cierra el issue)`.

El ciclo ya está descrito en `_docs/process.md`. Sesión limpia:

```text
/goal work through the backlog
```

El agente lee `AGENTS.md`, encuentra `process.md`, sigue el lifecycle y lanza los subagentes
de `_docs/team/`.

> ⚠️ **Aviso honesto del artículo:** esto consume *bastante* más tiempo y tokens que un loop
> simple con solo un ingeniero. En muchos casos no necesitas esta complejidad — un prompt
> directo o un loop simple es suficiente. Añade el grafo cuando el proyecto lo justifique,
> no por defecto.

---

# Consejos transversales

**Commitea después de cada decisión con sentido.** Es tu botón de deshacer. Con commits
frecuentes puedes revisar qué cambió el agente y volver al último estado bueno cuando se
descarrila — y se descarrila.

**Sesión nueva por rol.** No groomees, implementes y testees en la misma conversación. El
contexto de haber escrito el código contamina el juicio sobre ese código.

**Un issue cada vez.** Siempre.

**Lee el diff, no el resumen.** El resumen del agente de lo que hizo y lo que realmente hizo
no siempre coinciden. `git diff` es la fuente de verdad.

**Si un acceptance criterion está mal, se corrige en el issue**, no en el código. El ingeniero
tiene instrucción explícita de comentar en el issue en vez de "arreglarlo" por su cuenta.

**No dejes que añada dependencias sin preguntar.** Ya está como regla en `AGENTS.md`. Es la
vía más rápida a un `pyproject.toml` que no reconoces.

**Cuando corrijas dos veces lo mismo, es que falta contexto.** Actualiza `_docs/`, no repitas
la corrección.

---

# Checklist antes de decir "está terminado"

Fase 0–3 (arranque):

- [ ] `_docs/plan.md` describe usuarios, problema, flujo y no-objetivos
- [ ] Stack elegido *y justificado* — y sé leerlo
- [ ] Backlog en GitHub issues, `_docs/tasks.md` retirado
- [ ] `AGENTS.md` con comandos reales que he ejecutado yo
- [ ] Primer commit hecho antes de que el agente tocara nada

Proyecto final del curso (criterios de evaluación en `_docs/checklist-proyecto-final.md`):

- [ ] Frontend + backend + contrato OpenAPI + base de datos
- [ ] Tests que pasan · Docker · desplegado · CI/CD
- [ ] `_docs/ai-usage-report.md` relleno
- [ ] README con instrucciones reproducibles desde cero

---

## Mapa de la plantilla

```
AGENTS.md                       contexto durable — lo lee el agente en CADA sesión
CLAUDE.md                       una línea: @AGENTS.md (compatibilidad Claude Code)
.env.example                    variables de entorno, sin secretos reales
.gitignore
prompts/prompts.md              todos los prompts de esta guía, copy-paste, en orden
_docs/
  plan.md                       la spec (sale de la Fase 0)
  process.md                    roles, lifecycle, orquestador
  task-template.md              Goal / Acceptance criteria / Out of scope / Constraints
  testing-guidelines.md         se carga solo en tareas de tests
  design-system.md              se carga solo en tareas de UI
  api.md                        se carga solo en tareas de API
  ai-usage-report.md            entregable del curso
  checklist-proyecto-final.md   los 14 criterios de evaluación
  team/pm.md                    rol Product Manager
  team/software-engineer.md     rol Software Engineer
  team/qa-engineer.md           rol QA Engineer
.github/workflows/ci.yml        CI mínimo — adaptar al stack
```
