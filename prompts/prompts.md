# Prompts — copy & paste, en orden

Todos los prompts del flujo, listos para copiar. La explicación de cada uno está en
[`../START-HERE.md`](../START-HERE.md).

---

## 0. Brainstorming de la spec

**Dónde:** chat assistant (ChatGPT, Claude.ai). **No** el agente de código.
Mejor por dictado si puedes.

```text
I want to build <tu idea en una frase>.

Help me set the scope for this project precisely. I want to brainstorm with you
and understand how the tool should work. Give me options.

Ask me one question at a time and keep your output short.
```

Al terminar:

```text
Save everything to a markdown file that I can download.
```

→ guardar como `_docs/plan.md`

---

## 1. Elegir stack

**Dónde:** agente de código, sesión nueva.

```text
Read _docs/plan.md. Propose multiple options for the tech stack and
explain each option.

Don't write code yet.
```

---

## 2. Crear el backlog

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

### Ajustar el backlog (después de revisarlo tú)

```text
Merge tasks <N> and <M>: they are too small to be separate sessions.
Split task <K>: it does not fit in one session.
Remove tasks <...>: they are out of scope for the MVP.
```

---

## 3. Pasar el backlog a GitHub issues

Requiere `gh` CLI autenticado.

```text
Create a public GitHub repo for this project.
Move each task from _docs/tasks.md into a GitHub issue.
```

A partir de aquí, los issues son el backlog canónico.

---

## 4. Primera tarea

```text
Implement task 1.
```

---

## 5. Grooming (rol PM)

Un issue:

```text
Groom issue #4
```

Todos, con loop:

```text
/goal groom all issues
```

---

## 6. Implementación (rol Engineer)

Sesión nueva.

```text
Implement issue #2
```

Tras un FAIL de QA:

```text
Implement issue #2 again. Read the QA comment on the issue and fix
everything it reports. Do not change the acceptance criteria.
```

---

## 7. QA (rol QA Engineer)

Sesión nueva, distinta de la del ingeniero.

```text
Test issue #2
```

---

## 8. Orquestador — el backlog entero

Sesión limpia.

```text
/goal work through the backlog
```

---

## 9. Mantener vivos los documentos

Cuando hayas corregido al agente durante una sesión:

```text
Based on the corrections I made, find the relevant documents
and update them.

Commit the current work before changing the documents.
```

---

## Extras útiles

**Antes de aceptar un cambio grande:**

```text
Show me the diff and walk me through what changed and why.
Do not summarise what you intended to do - describe what the diff
actually does.
```

**Cuando el agente se descarrila:**

```text
Stop. Revert to the last commit. Explain what you were trying to do
and where it went wrong, before trying again.
```

**Condiciones de parada válidas para `/goal`** (comprobables por el modelo):

```text
/goal all tests pass
/goal all issues are groomed
/goal no file is over 200 lines
```

❌ No válidas: `make the code better`, `improve performance`, `clean it up`.
