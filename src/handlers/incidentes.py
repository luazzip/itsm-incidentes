"""API de Incidentes. Multi-tenant: la clave de todo es (tenant_id del token, incidente_id)."""
import uuid
from datetime import datetime, timedelta, timezone
from functools import reduce

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.common import (
    TABLA_COMENTARIOS, TABLA_INCIDENTES, ErrorApi, actualizar, ahora_dt, api, consultar, contexto,
    exigir_rol, iso, leer_body, leer_paginacion, log, ok, opcion, paginacion, path_param,
    publicar_evento, query_params, siguiente_numero, texto,
)

IMPACTOS = ('ALTO', 'MEDIO', 'BAJO')
URGENCIAS = ('ALTA', 'MEDIA', 'BAJA')
CATEGORIAS = ('RED', 'HARDWARE', 'SOFTWARE', 'ACCESOS', 'CORREO', 'OTROS')
PRIORIDADES = ('P1', 'P2', 'P3', 'P4')
ESTADOS = ('NUEVO', 'ASIGNADO', 'EN_PROGRESO', 'RESUELTO', 'CERRADO')
SLA_HORAS = {'P1': 4, 'P2': 8, 'P3': 24, 'P4': 72}
TRANSICIONES = {
    'NUEVO': ('ASIGNADO',),
    'ASIGNADO': ('EN_PROGRESO',),
    'EN_PROGRESO': ('RESUELTO',),
    'RESUELTO': ('CERRADO', 'EN_PROGRESO'),  # EN_PROGRESO = reabrir
    'CERRADO': (),
}
GESTORES = ('ADMIN', 'AGENTE')
CAMPOS_INTERNOS = ('estado_creado', 'asignado_creado', 'solicitante_creado')


def calcular_prioridad(impacto, urgencia):
    """Matriz impacto x urgencia: ALTO+ALTA=P1 ... BAJO+BAJA=P4."""
    return PRIORIDADES[min(IMPACTOS.index(impacto) + URGENCIAS.index(urgencia), 3)]


def publico(item):
    """Quita las claves internas de los GSI antes de responder."""
    salida = {k: v for k, v in item.items() if k not in CAMPOS_INTERNOS}
    salida.setdefault('asignado_a', None)
    salida.setdefault('problema_id', None)
    return salida


def clave(inc):
    return {'tenant_id': inc['tenant_id'], 'incidente_id': inc['incidente_id']}


def cargar(ctx, incidente_id):
    """Busca SOLO en el tenant del token: un ID de otro tenant simplemente no existe (404)."""
    item = TABLA_INCIDENTES.get_item(Key={'tenant_id': ctx['tenant_id'], 'incidente_id': incidente_id}).get('Item')
    if not item:
        raise ErrorApi(404, 'NO_ENCONTRADO', f'El incidente {incidente_id} no existe')
    if ctx['rol'] == 'SOLICITANTE' and item['solicitante_id'] != ctx['usuario_id']:
        raise ErrorApi(403, 'PROHIBIDO', 'Solo puedes ver tus propios incidentes')
    return item


def validar_adjuntos(adjuntos, tenant_id):
    """Keys de S3 devueltas por /archivos/presign-subida (Integrante 5). Deben estar en el prefijo del tenant."""
    if not isinstance(adjuntos, list) or len(adjuntos) > 10:
        raise ErrorApi(400, 'VALIDACION', 'adjuntos debe ser una lista de hasta 10 keys de S3')
    for key in adjuntos:
        if not isinstance(key, str) or not key.startswith(f'{tenant_id}/') or len(key) > 512:
            raise ErrorApi(400, 'VALIDACION', f'Cada adjunto debe ser una key de S3 que empiece con {tenant_id}/')
    return adjuntos


# ======================================================== POST /incidentes
@api
def crear(event, context):
    ctx = contexto(event)
    body = leer_body(event)
    tenant_id = ctx['tenant_id']
    titulo = texto(body, 'titulo', 3, 150)
    descripcion = texto(body, 'descripcion', 3, 2000)
    categoria = opcion(body.get('categoria'), 'categoria', CATEGORIAS)
    impacto = opcion(body.get('impacto'), 'impacto', IMPACTOS)
    urgencia = opcion(body.get('urgencia'), 'urgencia', URGENCIAS)
    adjuntos = validar_adjuntos(body.get('adjuntos', []), tenant_id)
    prioridad = calcular_prioridad(impacto, urgencia)

    momento = ahora_dt()
    creado_en = iso(momento)
    incidente_id = f'inc-{tenant_id}-{siguiente_numero(tenant_id, "incidente"):06d}'
    item = {
        'tenant_id': tenant_id,
        'incidente_id': incidente_id,
        'titulo': titulo,
        'descripcion': descripcion,
        'categoria': categoria,
        'impacto': impacto,
        'urgencia': urgencia,
        'prioridad': prioridad,
        'estado': 'NUEVO',
        'solicitante_id': ctx['usuario_id'],
        'escalado': False,
        'adjuntos': adjuntos,
        'sla_vence_en': iso(momento + timedelta(hours=SLA_HORAS[prioridad])),
        'creado_en': creado_en,
        'actualizado_en': creado_en,
        'estado_creado': f'NUEVO#{creado_en}',
        'solicitante_creado': f"{ctx['usuario_id']}#{creado_en}",
    }
    TABLA_INCIDENTES.put_item(Item=item, ConditionExpression='attribute_not_exists(incidente_id)')
    publicar_evento('incidente.creado', tenant_id, {
        'incidente_id': incidente_id, 'titulo': titulo, 'categoria': categoria,
        'prioridad': prioridad, 'solicitante_id': ctx['usuario_id'],
    })
    log('INFO', accion='incidente_creado', tenant_id=tenant_id, incidente_id=incidente_id, prioridad=prioridad)
    return ok(publico(item), 201)


# ======================================================== GET /incidentes
@api
def listar(event, context):
    ctx = contexto(event)
    q = query_params(event)
    limite, inicio = leer_paginacion(q, {'tenant_id': ctx['tenant_id']})
    estado, prioridad, asignado_a = q.get('estado'), q.get('prioridad'), q.get('asignado_a')
    if estado:
        opcion(estado, 'estado', ESTADOS)
    if prioridad:
        opcion(prioridad, 'prioridad', PRIORIDADES)

    del_tenant = Key('tenant_id').eq(ctx['tenant_id'])
    params = {'Limit': limite, 'ScanIndexForward': False}  # mas recientes primero
    filtros = []
    if ctx['rol'] == 'SOLICITANTE':  # solo los suyos (GSI3)
        params['IndexName'] = 'GSI3'
        params['KeyConditionExpression'] = del_tenant & Key('solicitante_creado').begins_with(ctx['usuario_id'] + '#')
        if estado:
            filtros.append(Attr('estado').eq(estado))
        if asignado_a:
            filtros.append(Attr('asignado_a').eq(asignado_a))
    elif estado:  # por estado (GSI1)
        params['IndexName'] = 'GSI1'
        params['KeyConditionExpression'] = del_tenant & Key('estado_creado').begins_with(estado + '#')
        if asignado_a:
            filtros.append(Attr('asignado_a').eq(asignado_a))
    elif asignado_a:  # mis incidentes / por agente (GSI2)
        params['IndexName'] = 'GSI2'
        params['KeyConditionExpression'] = del_tenant & Key('asignado_creado').begins_with(asignado_a + '#')
    else:  # todos los del tenant (tabla principal)
        params['KeyConditionExpression'] = del_tenant
    if prioridad:
        filtros.append(Attr('prioridad').eq(prioridad))
    if filtros:
        params['FilterExpression'] = reduce(lambda a, b: a & b, filtros)
    if inicio:
        params['ExclusiveStartKey'] = inicio

    r = consultar(TABLA_INCIDENTES, params)
    return ok([publico(i) for i in r['Items']], paginacion=paginacion(limite, r))


# ======================================================== GET /incidentes/{id}
@api
def obtener(event, context):
    ctx = contexto(event)
    return ok(publico(cargar(ctx, path_param(event, 'id'))))


# ======================================================== PUT /incidentes/{id}
@api
def editar(event, context):
    ctx = contexto(event)
    exigir_rol(ctx, *GESTORES)
    inc = cargar(ctx, path_param(event, 'id'))
    body = leer_body(event)
    cambios = {}
    if 'titulo' in body:
        cambios['titulo'] = texto(body, 'titulo', 3, 150)
    if 'descripcion' in body:
        cambios['descripcion'] = texto(body, 'descripcion', 3, 2000)
    if 'categoria' in body:
        cambios['categoria'] = opcion(body['categoria'], 'categoria', CATEGORIAS)
    if not cambios:
        raise ErrorApi(400, 'VALIDACION', 'Envia al menos titulo, descripcion o categoria')
    if inc['estado'] == 'CERRADO':
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'No se puede editar un incidente CERRADO')
    cambios['actualizado_en'] = iso(ahora_dt())
    nuevo = actualizar(TABLA_INCIDENTES, clave(inc), cambios, condicion={'estado': inc['estado']})
    return ok(publico(nuevo))


# ======================================================== PATCH /incidentes/{id}/estado
@api
def cambiar_estado(event, context):
    ctx = contexto(event)
    exigir_rol(ctx, *GESTORES)
    inc = cargar(ctx, path_param(event, 'id'))
    nuevo_estado = opcion(leer_body(event).get('estado'), 'estado', ESTADOS)
    actual = inc['estado']
    if nuevo_estado not in TRANSICIONES[actual]:
        permitidos = ', '.join(TRANSICIONES[actual]) or 'ninguno'
        raise ErrorApi(409, 'ESTADO_INVALIDO', f'No se puede pasar de {actual} a {nuevo_estado}. Permitidos: {permitidos}')
    if nuevo_estado == 'ASIGNADO' and not inc.get('asignado_a'):
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'Primero asigna un agente con PATCH /incidentes/{id}/asignar')

    momento = iso(ahora_dt())
    cambios = {'estado': nuevo_estado, 'estado_creado': f"{nuevo_estado}#{inc['creado_en']}", 'actualizado_en': momento}
    quitar = []
    if nuevo_estado == 'RESUELTO':
        cambios['resuelto_en'] = momento
    elif nuevo_estado == 'CERRADO':
        cambios['cerrado_en'] = momento
    elif actual == 'RESUELTO':  # reabierto
        quitar.append('resuelto_en')
    nuevo = actualizar(TABLA_INCIDENTES, clave(inc), cambios, quitar, condicion={'estado': actual})

    if nuevo_estado == 'RESUELTO':
        publicar_evento('incidente.resuelto', ctx['tenant_id'], {
            'incidente_id': inc['incidente_id'], 'titulo': inc['titulo'], 'resuelto_por': ctx['usuario_id'],
        })
    log('INFO', accion='estado_cambiado', tenant_id=ctx['tenant_id'], incidente_id=inc['incidente_id'],
        de=actual, a=nuevo_estado)
    return ok(publico(nuevo))


# ======================================================== PATCH /incidentes/{id}/asignar
@api
def asignar(event, context):
    ctx = contexto(event)
    exigir_rol(ctx, *GESTORES)
    inc = cargar(ctx, path_param(event, 'id'))
    asignado_a = texto(leer_body(event), 'asignado_a', 5, 60)
    if not asignado_a.startswith(f"usr-{ctx['tenant_id']}-"):
        raise ErrorApi(400, 'VALIDACION', f"asignado_a debe ser un usuario de tu tenant (usr-{ctx['tenant_id']}-NNNNN)")
    if inc['estado'] in ('RESUELTO', 'CERRADO'):
        raise ErrorApi(409, 'ESTADO_INVALIDO', f"No se puede asignar un incidente {inc['estado']}")

    cambios = {
        'asignado_a': asignado_a,
        'asignado_creado': f"{asignado_a}#{inc['creado_en']}",
        'actualizado_en': iso(ahora_dt()),
    }
    if inc['estado'] == 'NUEVO':
        cambios['estado'] = 'ASIGNADO'
        cambios['estado_creado'] = f"ASIGNADO#{inc['creado_en']}"
    nuevo = actualizar(TABLA_INCIDENTES, clave(inc), cambios, condicion={'estado': inc['estado']})
    log('INFO', accion='incidente_asignado', tenant_id=ctx['tenant_id'], incidente_id=inc['incidente_id'],
        asignado_a=asignado_a)
    return ok(publico(nuevo))


# ======================================================== POST /incidentes/{id}/escalar-problema
@api
def escalar_problema(event, context):
    ctx = contexto(event)
    exigir_rol(ctx, *GESTORES)
    inc = cargar(ctx, path_param(event, 'id'))
    if inc.get('escalado'):
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'Este incidente ya fue escalado a problema')
    if inc['estado'] == 'CERRADO':
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'No se puede escalar un incidente CERRADO')
    motivo = texto(leer_body(event), 'motivo', 3, 1000, requerido=False)

    momento = iso(ahora_dt())
    cambios = {'escalado': True, 'escalado_en': momento, 'escalado_por': ctx['usuario_id'], 'actualizado_en': momento}
    nuevo = actualizar(TABLA_INCIDENTES, clave(inc), cambios, condicion={'estado': inc['estado']})
    publicar_evento('incidente.escalado_a_problema', ctx['tenant_id'], {
        'incidente_id': inc['incidente_id'], 'titulo': inc['titulo'], 'descripcion': inc['descripcion'],
        'categoria': inc['categoria'], 'prioridad': inc['prioridad'], 'solicitante_id': inc['solicitante_id'],
        'escalado_por': ctx['usuario_id'], 'motivo': motivo,
    })
    return ok(publico(nuevo))


# ======================================================== POST /incidentes/{id}/comentarios
@api
def crear_comentario(event, context):
    ctx = contexto(event)
    inc = cargar(ctx, path_param(event, 'id'))
    if inc['estado'] == 'CERRADO':
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'No se puede comentar un incidente CERRADO')
    contenido = texto(leer_body(event), 'texto', 1, 2000)
    momento = datetime.now(timezone.utc)
    comentario = {
        'tenant_incidente': f"{ctx['tenant_id']}#{inc['incidente_id']}",
        'comentario_id': f"com-{momento.strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:6]}",  # ordena por fecha
        'tenant_id': ctx['tenant_id'],
        'incidente_id': inc['incidente_id'],
        'autor_id': ctx['usuario_id'],
        'autor_rol': ctx['rol'],
        'texto': contenido,
        'creado_en': iso(momento),
    }
    TABLA_COMENTARIOS.put_item(Item=comentario)
    return ok(comentario, 201)


# ======================================================== GET /incidentes/{id}/comentarios
@api
def listar_comentarios(event, context):
    ctx = contexto(event)
    inc = cargar(ctx, path_param(event, 'id'))
    pk = f"{ctx['tenant_id']}#{inc['incidente_id']}"
    limite, inicio = leer_paginacion(query_params(event), {'tenant_incidente': pk})
    params = {'KeyConditionExpression': Key('tenant_incidente').eq(pk), 'ScanIndexForward': True, 'Limit': limite}
    if inicio:
        params['ExclusiveStartKey'] = inicio
    r = consultar(TABLA_COMENTARIOS, params)
    return ok(r['Items'], paginacion=paginacion(limite, r))


# ======================================================== DELETE /incidentes/{id}
@api
def eliminar(event, context):
    ctx = contexto(event)
    exigir_rol(ctx, 'ADMIN')
    inc = cargar(ctx, path_param(event, 'id'))
    if inc['estado'] != 'NUEVO':
        raise ErrorApi(409, 'ESTADO_INVALIDO', 'Solo se pueden eliminar incidentes en estado NUEVO')
    try:
        TABLA_INCIDENTES.delete_item(Key=clave(inc), ConditionExpression=Attr('estado').eq('NUEVO'))
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ErrorApi(409, 'CONFLICTO', 'El incidente cambio de estado, ya no se puede eliminar')
        raise
    # Borra tambien sus comentarios
    pk = f"{ctx['tenant_id']}#{inc['incidente_id']}"
    params = {'KeyConditionExpression': Key('tenant_incidente').eq(pk), 'ProjectionExpression': 'tenant_incidente, comentario_id'}
    with TABLA_COMENTARIOS.batch_writer() as lote:
        while True:
            r = TABLA_COMENTARIOS.query(**params)
            for c in r['Items']:
                lote.delete_item(Key=c)
            if 'LastEvaluatedKey' not in r:
                break
            params['ExclusiveStartKey'] = r['LastEvaluatedKey']
    log('INFO', accion='incidente_eliminado', tenant_id=ctx['tenant_id'], incidente_id=inc['incidente_id'])
    return ok({'incidente_id': inc['incidente_id'], 'eliminado': True})
