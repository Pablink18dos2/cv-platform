# Checklist del proyecto final

Los 14 criterios de evaluación por peer review del curso, en formato marcable.
Máximo **30 puntos**. Fuente: [`project/README.md`](../../project/README.md) del repo del curso.

> ⚠️ Los criterios 2026 están marcados como **draft** en el repo: pueden cambiar antes
> del 31 de agosto. Revísalos cuando arranque la cohorte.

## Contenido esperado del repo

```text
README.md
product-spec.md              ← nuestro _docs/plan.md
AGENTS.md
frontend/
backend/
openapi.yaml
docker-compose.yml
.github/workflows/
docs/                        ← nuestro _docs/
security/
ops/
```

Si completas el Módulo 3, además:

```text
agent-capabilities/
agent-hooks/
mcp-server/
plugins/ o custom-agent/
docs/agent-extension-pack.md
docs/permissions.md
```

## Criterios

**1. Problem description** (0–2)
- [ ] El README describe el problema, la funcionalidad y el comportamiento esperado → 2

**2. AI-assisted development workflow** (0–2)
- [ ] Documentado el workflow: prompts/delegación, ficheros de contexto, revisión manual y verificación → 2
- ↳ esto sale de `_docs/ai-usage-report.md` si lo rellenas sobre la marcha

**3. Technologies and architecture** (0–2)
- [ ] Frontend, backend, BD, contenedores, CI/CD y cómo encajan entre sí → 2

**4. Frontend** (0–3)
- [ ] Funcional y bien estructurado, con la comunicación con backend centralizada → 2
- [ ] + tests de la lógica principal con instrucciones para ejecutarlos → 3

**5. API contract** (0–2)
- [ ] `openapi.yaml` refleja las necesidades del frontend y es el contrato del backend → 2

**6. Backend** (0–3)
- [ ] Bien estructurado y sigue el OpenAPI → 2
- [ ] + tests de la funcionalidad principal → 3

**7. Database** (0–2)
- [ ] Integrada, soporta distintos entornos y está documentada → 2

**8. Containerization** (0–2)
- [ ] El sistema completo arranca con Docker/Docker Compose e instrucciones claras → 2

**9. Integration testing** (0–2)
- [ ] Separados de los unit tests, cubren flujos clave, documentados → 2

**10. Deployment** (0–2)
- [ ] Desplegado en cloud con URL funcionando o prueba clara del despliegue → 2

**11. CI/CD** (0–2)
- [ ] El pipeline ejecuta tests **y** despliega cuando pasan → 2

**12. Agent extension pack** (0–2) · *Módulo 3*
- [ ] Instrucciones de proyecto + workflow reutilizable + subagente + MCP tool/server + hook/guardrail + notas de permisos, todo documentado → 2

**13. Security, audit & DevOps hardening** (0–2) · *Módulo 4*
- [ ] Output de auditoría de PR + hallazgos de scan de seguridad + notas de seguridad de agentes/extensiones + diagnóstico operacional + política de datos/herramientas IA → 2

**14. Reproducibility** (0–2)
- [ ] Instrucciones claras para instalar, ejecutar, testear y desplegar de punta a punta → 2

---

**Total: ___ / 30**

> 💡 Los criterios 12 y 13 son los que más gente se deja: dependen de los Módulos 3 y 4,
> que se dan al final. Reserva tiempo para ellos.
