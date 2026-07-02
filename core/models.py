from django.contrib.auth.models import User
from django.core.validators import FileExtensionValidator
from django.db import models
import re


RECORD_ATTACHMENT_EXTENSIONS = [
    'pdf',
    'doc',
    'docx',
    'xls',
    'xlsx',
    'csv',
    'ppt',
    'pptx',
    'txt',
    'png',
    'jpg',
    'jpeg',
    'zip',
]


def aca_attachment_upload_to(instance, filename):
    return f'aca_adjuntos/{instance.criticidad_id}/{filename}'


def rcm_attachment_upload_to(instance, filename):
    return f'rcm_adjuntos/{instance.rcm_id}/{filename}'


class ActiveUsuarioManager(models.Manager):
    def get_queryset(self):
        return super().get_queryset().filter(archivo_eliminacion__isnull=True)


class AllUsuarioManager(models.Manager):
    pass


class BaseUnmanagedModel(models.Model):
    class Meta:
        abstract = True


class Empresa(BaseUnmanagedModel):
    ESTADO_CHOICE = [
        ("Activo", "Activo"),
        ("Inactivo", "Inactivo"),
        
    ]
    nombre = models.CharField(max_length=200)
    sigla = models.CharField(max_length=50)
    estado = models.CharField(max_length=50, choices=ESTADO_CHOICE)
    logo = models.BinaryField(blank=True, null=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'empresa'
        ordering = ['nombre']

    def __str__(self):
        return f'{self.nombre} ({self.sigla})'

class Cargo(BaseUnmanagedModel):
    nombre_cargo = models.CharField(max_length=150)
    area = models.CharField(max_length=150)
    jefatura = models.CharField(max_length=150)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'cargo'
        ordering = ['nombre_cargo']

    def __str__(self):
        return self.nombre_cargo

class Estrategia(BaseUnmanagedModel):
    nombre = models.CharField(max_length=200)
    version = models.DecimalField(max_digits=4, decimal_places=1)
    activa = models.BooleanField(default=True)
    descripcion = models.TextField(blank=True)
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='estrategias')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'estrategia'
        ordering = ['empresa__nombre', 'nombre']

    def __str__(self):
        return f'{self.nombre} v{self.version}'

class Usuario(BaseUnmanagedModel):
    nombre_completo = models.CharField(max_length=200)
    correo_corporativo = models.EmailField(max_length=254)
    auth_user = models.OneToOneField(User, on_delete=models.DO_NOTHING, db_column='auth_user_id', blank=True, null=True, related_name='perfil_reliability')
    cargo = models.ForeignKey(Cargo, on_delete=models.DO_NOTHING, db_column='cargo_id', blank=True, null=True, related_name='usuarios')
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', blank=True, null=True, related_name='usuarios')

    objects = ActiveUsuarioManager()
    all_objects = AllUsuarioManager()

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'usuario'
        ordering = ['nombre_completo']
        default_manager_name = 'objects'
        base_manager_name = 'all_objects'

    @property
    def is_deleted(self):
        return hasattr(self, 'archivo_eliminacion')

    def __str__(self):
        return self.nombre_completo


class UsuarioEliminado(BaseUnmanagedModel):
    usuario = models.OneToOneField(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', related_name='archivo_eliminacion', primary_key=True)
    auth_user = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='auth_user_id', blank=True, null=True, related_name='usuarios_eliminados')
    nombre_completo = models.CharField(max_length=200)
    correo_corporativo = models.EmailField(max_length=254, blank=True)
    cargo = models.ForeignKey(Cargo, on_delete=models.DO_NOTHING, db_column='cargo_id', blank=True, null=True, related_name='usuarios_eliminados')
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', blank=True, null=True, related_name='usuarios_eliminados')
    eliminado_en = models.DateTimeField()
    eliminado_por = models.ForeignKey(User, on_delete=models.DO_NOTHING, db_column='eliminado_por_id', blank=True, null=True, related_name='eliminaciones_de_usuario')
    motivo = models.CharField(max_length=255, blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'usuario_eliminado'
        ordering = ['-eliminado_en', 'nombre_completo']

    def __str__(self):
        return f'{self.nombre_completo} [eliminado]'


class Servicio(BaseUnmanagedModel):
    STATUS_CHOICES = [
        ('activo', 'Activo'),
        ('en_curso', 'En curso'),
        ('cerrado', 'Cerrado'),
        ('pausado', 'Pausado'),
    ]

    codigo_servicio = models.CharField(max_length=100)
    descripcion = models.TextField(blank=True)
    fecha_inicio = models.DateField(blank=True, null=True)
    fecha_fin = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='activo')
    creado_en = models.DateTimeField()
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='servicios')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', blank=True, null=True, related_name='servicios')
    matriz_aca_activa = models.ForeignKey(
        'MatrizRiesgo',
        on_delete=models.SET_NULL,
        db_column='matriz_aca_activa_id',
        blank=True,
        null=True,
        related_name='servicios_aca_activa',
    )
    creado_por_usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='creado_por_usuario_id', blank=True, null=True, related_name='servicios_creados', verbose_name='Administrador')
    responsable_usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='responsable_usuario_id', blank=True, null=True, related_name='servicios_responsables', verbose_name='Responsable')
    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'servicio'
        ordering = ['-creado_en', 'codigo_servicio']

    def __str__(self):
        return self.codigo_servicio

class AccesoUsuario(BaseUnmanagedModel):
    puede_ver = models.BooleanField(default=True)
    puede_editar = models.BooleanField(default=False)
    puede_ver_todo = models.BooleanField(default=False)
    creado_en = models.DateTimeField()
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='accesos')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', blank=True, null=True, related_name='accesos')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', blank=True, null=True, related_name='accesos')
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', related_name='accesos')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'accesousuario'
        ordering = ['-creado_en']

    def __str__(self):
        scope = self.servicio or self.estrategia or self.empresa
        return f'{self.usuario} -> {scope}'


class Componente(BaseUnmanagedModel):
    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'componente'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


def _technical_segment(value):
    value = (value or '').strip().upper()
    value = re.sub(r'[^A-Z0-9]+', '-', value)
    return value.strip('-') or 'SIN-CODIGO'


def _equipment_tag_from_ut(value):
    parts = [part for part in str(value or '').strip().split('-') if part]
    if not parts:
        return ''
    tag = parts[-1].strip().upper()
    normalized = tag.lstrip('0')
    return normalized or tag


class NivelJerarquia(BaseUnmanagedModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='niveles_jerarquia')
    nombre = models.CharField(max_length=100)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'niveljerarquia'
        ordering = ['empresa__nombre', 'orden', 'nombre']
        unique_together = (
            ('empresa', 'orden'),
            ('empresa', 'nombre'),
        )

    def __str__(self):
        return f'{self.empresa.sigla} / {self.orden}. {self.nombre}'


class NodoJerarquia(BaseUnmanagedModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='nodos_jerarquia')
    nivel = models.ForeignKey(NivelJerarquia, on_delete=models.DO_NOTHING, db_column='nivel_id', related_name='nodos')
    parent = models.ForeignKey('self', on_delete=models.DO_NOTHING, db_column='parent_id', blank=True, null=True, related_name='hijos')
    codigo = models.CharField(max_length=50)
    nombre = models.CharField(max_length=200)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'nodojerarquia'
        ordering = ['empresa__nombre', 'nivel__orden', 'orden', 'codigo', 'nombre']
        unique_together = (
            ('empresa', 'parent', 'codigo'),
        )

    def __str__(self):
        return self.ut

    def path_nodes(self):
        nodes = []
        current = self
        seen = set()
        while current and current.pk not in seen:
            seen.add(current.pk)
            nodes.append(current)
            current = current.parent
        return list(reversed(nodes))

    @property
    def depth(self):
        return len(self.path_nodes()) - 1

    @property
    def ut(self):
        return '-'.join(_technical_segment(node.codigo) for node in self.path_nodes())

    @property
    def ruta_nombre(self):
        return ' > '.join(f'{node.nivel.nombre}: {node.codigo} - {node.nombre}' for node in self.path_nodes())


class ValorNivelJerarquia(BaseUnmanagedModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='valores_nivel_jerarquia')
    nivel = models.ForeignKey(NivelJerarquia, on_delete=models.DO_NOTHING, db_column='nivel_id', related_name='valores_simples')
    codigo = models.CharField(max_length=50)
    nombre = models.CharField(max_length=200)
    orden = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'valorniveljerarquia'
        ordering = ['empresa__nombre', 'nivel__orden', 'orden', 'codigo', 'nombre']
        unique_together = (
            ('empresa', 'nivel', 'codigo'),
        )
        indexes = [
            models.Index(fields=['empresa', 'nivel', 'activo']),
            models.Index(fields=['codigo']),
            models.Index(fields=['nombre']),
        ]

    def __str__(self):
        return f'{self.empresa.sigla} / {self.nivel.nombre}: {self.codigo} - {self.nombre}'


class Equipo(BaseUnmanagedModel):
    tag_equipo = models.CharField(max_length=100)
    nombre_equipo = models.CharField(max_length=200)
    ut = models.CharField(max_length=200)
    descripcion_ut = models.CharField(max_length=255)
    nodo = models.ForeignKey(NodoJerarquia, on_delete=models.DO_NOTHING, db_column='nodo_id', blank=True, null=True, related_name='equipos')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'equipo'
        ordering = ['tag_equipo', 'nombre_equipo']

    @property
    def tag_display(self):
        return self.tag_equipo or _equipment_tag_from_ut(self.ut)

    @property
    def ut_display(self):
        tag = _technical_segment(self.tag_equipo) if self.tag_equipo else ''
        if self.nodo_id and tag:
            return f'{self.nodo.ut}-{tag}'
        return self.ut

    def __str__(self):
        return f'{self.tag_display} - {self.nombre_equipo}'


class ComponenteEquipo(BaseUnmanagedModel):
    componente = models.ForeignKey(Componente, on_delete=models.DO_NOTHING, db_column='componente_id', related_name='componentes_equipo')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='componentes')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'componenteequipo'
        ordering = ['equipo_id', 'componente_id']

    def __str__(self):
        return f'{self.equipo} / {self.componente}'


class ServicioEquipo(BaseUnmanagedModel):
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='servicios_equipo')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='equipos_servicio')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'servicioequipo'
        ordering = ['servicio_id', 'equipo_id']

    def __str__(self):
        return f'{self.servicio} / {self.equipo}'


class FamiliaEquipo(BaseUnmanagedModel):
    nombre = models.CharField(max_length=150)
    descripcion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    creado_en = models.DateTimeField()
    actualizado = models.DateTimeField()
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='familias_equipo')
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='familias_equipo')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'familiaequipo'
        ordering = ['servicio_id', 'nombre']
        unique_together = (('servicio', 'nombre'),)
        indexes = [
            models.Index(fields=['servicio', 'activa'], name='idx_familiaequipo_servicio'),
            models.Index(fields=['nombre'], name='idx_familiaequipo_nombre'),
        ]

    def __str__(self):
        return f'{self.servicio.codigo_servicio} / {self.nombre}'


class FamiliaEquipoItem(BaseUnmanagedModel):
    familia = models.ForeignKey(FamiliaEquipo, on_delete=models.CASCADE, db_column='familia_id', related_name='items')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='familias_item')
    orden = models.PositiveIntegerField(default=0)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'familiaequipoitem'
        ordering = ['familia_id', 'orden', 'equipo__tag_equipo', 'equipo__nombre_equipo']
        unique_together = (('familia', 'equipo'),)
        indexes = [
            models.Index(fields=['familia'], name='idx_familiaitem_familia'),
            models.Index(fields=['equipo'], name='idx_familiaitem_equipo'),
        ]

    def __str__(self):
        return f'{self.familia} / {self.equipo}'


class EscenarioFalla(BaseUnmanagedModel):
    nombre = models.CharField(max_length=200)
    activo = models.BooleanField(default=True)
    creado_en = models.DateTimeField()
    actualizado = models.DateTimeField()
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='escenarios_falla')
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='escenarios_falla')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'escenariofalla'
        ordering = ['servicio_id', 'nombre']
        unique_together = (('servicio', 'nombre'),)
        indexes = [
            models.Index(fields=['servicio', 'activo'], name='idx_escenariofalla_servicio'),
            models.Index(fields=['nombre'], name='idx_escenariofalla_nombre'),
        ]

    def __str__(self):
        return f'{self.servicio.codigo_servicio} / {self.nombre}'


class Carga(BaseUnmanagedModel):
    STATUS_COMPLETO = 'Completo'
    STATUS_INCOMPLETO = 'Incompleto'
    STATUS_CHOICES = [
        (STATUS_COMPLETO, 'Completo'),
        (STATUS_INCOMPLETO, 'Incompleto'),
    ]

    fecha_analisis = models.DateField()
    version_carga = models.DecimalField(max_digits=4, decimal_places=1)
    origen = models.CharField(max_length=200)
    status = models.CharField('Estado', max_length=20, choices=STATUS_CHOICES, default=STATUS_INCOMPLETO)
    creado_en = models.DateTimeField()
    actualizado = models.DateTimeField()
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='cargas')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='cargas')
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='cargas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'acacarga'
        ordering = ['-fecha_analisis', '-creado_en']

    def __str__(self):
        return f'Carga {self.id} - {self.fecha_analisis}'


class Criticidad(BaseUnmanagedModel):
    escenario_falla = models.TextField(blank=True)
    observacion = models.TextField('Observación', blank=True, default='')
    frecuencia_original = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    frecuencia_normalizada = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    valor_cons_total = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    indicador_criticidad = models.CharField(max_length=100)
    valor_criticidad_equipo = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    criticidad_final = models.CharField(max_length=30)
    trazabilidad_criticidad_json = models.TextField(blank=True, default='')
    creado_en = models.DateTimeField()
    aca_carga = models.ForeignKey(Carga, on_delete=models.DO_NOTHING, db_column='aca_carga_id', related_name='criticidades')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', blank=True, null=True, related_name='criticidades')
    matriz = models.ForeignKey(
        'MatrizRiesgo',
        on_delete=models.SET_NULL,
        db_column='matriz_id',
        blank=True,
        null=True,
        related_name='criticidades_aca',
    )
    matriz_celda = models.ForeignKey(
        'MatrizRiesgoCelda',
        on_delete=models.SET_NULL,
        db_column='matriz_celda_id',
        blank=True,
        null=True,
        related_name='criticidades_aca',
    )

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'criticidad'
        ordering = ['-creado_en', 'id']

    def __str__(self):
        return f'Criticidad {self.id} - {self.equipo}'


class CriticidadAdjunto(BaseUnmanagedModel):
    criticidad = models.ForeignKey(Criticidad, on_delete=models.CASCADE, db_column='criticidad_id', related_name='adjuntos')
    archivo = models.FileField(
        upload_to=aca_attachment_upload_to,
        validators=[FileExtensionValidator(RECORD_ATTACHMENT_EXTENSIONS)],
    )
    nombre_original = models.CharField(max_length=255)
    creado_en = models.DateTimeField()
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='adjuntos_aca')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'criticidadadjunto'
        ordering = ['-creado_en', '-id']
        indexes = [
            models.Index(fields=['criticidad'], name='idx_critadj_criticidad'),
            models.Index(fields=['creado_en'], name='idx_critadj_creado'),
        ]

    def __str__(self):
        return self.nombre_original or f'Adjunto ACA {self.id}'


class Dimension(BaseUnmanagedModel):
    TIPO_FUNCIONAL_CHOICES = [
        ('impacto', 'Impacto'),
        ('probabilidad', 'Probabilidad'),
        ('resultado', 'Resultado'),
        ('catalogo', 'Catálogo'),
    ]
    TIPO_DATO_CHOICES = [
        ('numerico', 'Numérico'),
        ('booleano', 'Booleano'),
        ('texto', 'Texto'),
        ('ordinal', 'Ordinal'),
        ('tabla', 'Tabla / catálogo'),
    ]
    TIPO_CALCULO_CHOICES = [
        ('', 'Sin cálculo'),
        ('suma', 'Suma'),
        ('resta', 'Resta'),
        ('multiplicacion', 'Multiplicación'),
        ('division', 'División'),
        ('maximo', 'Máximo'),
        ('minimo', 'Mínimo'),
    ]

    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    tipo_funcional = models.CharField(max_length=20)
    tipo_dato = models.CharField(max_length=20)
    tipo_calculo = models.CharField(max_length=30, blank=True, null=True)
    config_calculo = models.TextField(blank=True, null=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'dimension'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class EstrategiaDimension(BaseUnmanagedModel):
    PROCESO_ACA = 'aca'
    PROCESO_FMECA = 'fmeca'
    PROCESO_RCM_LEGACY = 'rcm'
    PROCESO_RCM = PROCESO_FMECA
    PROCESO_AMBOS = 'ambos'
    PROCESO_FMECA_ALIASES = (PROCESO_FMECA, PROCESO_RCM_LEGACY, 'rcm_fmea', 'global')
    PROCESO_USO_CHOICES = [
        (PROCESO_ACA, 'ACA'),
        (PROCESO_FMECA, 'FMECA'),
        (PROCESO_AMBOS, 'ACA y FMECA'),
    ]

    orden = models.PositiveIntegerField()
    obligatorio = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)
    considerar_avance_aca = models.BooleanField('Considerar en avance ACA', default=True)
    visible_en_listado_aca = models.BooleanField('Visible en listado ACA', default=True)
    considerar_avance_fmeca = models.BooleanField('Considerar en avance FMECA', default=True)
    visible_en_listado_fmeca = models.BooleanField('Visible en listado FMECA', default=True)
    proceso_uso = models.CharField(max_length=10, choices=PROCESO_USO_CHOICES, default=PROCESO_ACA)
    dimension = models.ForeignKey(Dimension, on_delete=models.DO_NOTHING, db_column='dimension_id', related_name='estrategias_dimension')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='dimensiones_estrategia')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'estrategiadimension'
        ordering = ['estrategia_id', 'orden']

    def __str__(self):
        return f'{self.estrategia} / {self.dimension}'


class RCM(BaseUnmanagedModel):
    ESTADO_CHOICES = Carga.STATUS_CHOICES

    carga = models.OneToOneField(Carga, on_delete=models.DO_NOTHING, db_column='carga_id', related_name='rcm')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='registros_rcm')
    criticidad = models.IntegerField(blank=True, null=True)
    trazabilidad_criticidad_json = models.TextField(blank=True, default='')
    fecha_analisis = models.DateField()
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default=Carga.STATUS_INCOMPLETO)
    componente = models.CharField('Componente', max_length=255, blank=True, null=True)
    funcion = models.TextField('Función', blank=True, default='')
    falla_funcional = models.TextField()
    modo_de_falla = models.TextField()
    causa = models.TextField(blank=True, null=True)
    efecto = models.TextField()
    observacion = models.TextField('Observación', blank=True, default='')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'rcm'
        ordering = ['-fecha_analisis', 'id']
        indexes = [
            models.Index(fields=['equipo'], name='idx_rcm_equipo'),
            models.Index(fields=['estado'], name='idx_rcm_estado'),
            models.Index(fields=['fecha_analisis'], name='idx_rcm_fecha'),
            models.Index(fields=['criticidad'], name='idx_rcm_criticidad'),
        ]

    @property
    def tipo_analisis(self):
        return 'FMECA' if self.criticidad is not None else 'FMEA'

    def __str__(self):
        return f'RCM {self.id} - {self.equipo}'


class RCMCampoOpcion(BaseUnmanagedModel):
    CAMPO_FALLA_FUNCIONAL = 'falla_funcional'
    CAMPO_MODO_DE_FALLA = 'modo_de_falla'
    CAMPO_EFECTO = 'efecto'
    CAMPO_CHOICES = [
        (CAMPO_FALLA_FUNCIONAL, 'Falla funcional'),
        (CAMPO_MODO_DE_FALLA, 'Modo de falla'),
        (CAMPO_EFECTO, 'Efecto'),
    ]

    servicio = models.ForeignKey(
        Servicio,
        on_delete=models.CASCADE,
        db_column='servicio_id',
        related_name='opciones_campos_rcm',
    )
    campo = models.CharField(max_length=32, choices=CAMPO_CHOICES)
    valor = models.TextField()
    clave_normalizada = models.CharField(max_length=64)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'rcmcampoopcion'
        ordering = ['campo', 'valor']
        constraints = [
            models.UniqueConstraint(
                fields=['servicio', 'campo', 'clave_normalizada'],
                name='uq_rcm_opcion_servicio_campo_clave',
            ),
        ]

    def save(self, *args, **kwargs):
        from rcm.field_options import rcm_field_option_key

        self.clave_normalizada = rcm_field_option_key(self.valor)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.get_campo_display()}: {self.valor}'


class RCMAdjunto(BaseUnmanagedModel):
    rcm = models.ForeignKey(RCM, on_delete=models.CASCADE, db_column='rcm_id', related_name='adjuntos')
    archivo = models.FileField(
        upload_to=rcm_attachment_upload_to,
        validators=[FileExtensionValidator(RECORD_ATTACHMENT_EXTENSIONS)],
    )
    nombre_original = models.CharField(max_length=255)
    creado_en = models.DateTimeField()
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='adjuntos_rcm')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'rcmadjunto'
        ordering = ['-creado_en', '-id']
        indexes = [
            models.Index(fields=['rcm'], name='idx_rcmadj_rcm'),
            models.Index(fields=['creado_en'], name='idx_rcmadj_creado'),
        ]

    def __str__(self):
        return self.nombre_original or f'Adjunto RCM {self.id}'


class FMEA_FMECA(BaseUnmanagedModel):
    rcm = models.OneToOneField(RCM, on_delete=models.DO_NOTHING, db_column='rcm_id', related_name='fmea_fmeca')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'fmea_fmeca'
        ordering = ['id']

    def __str__(self):
        return f'{self.rcm.tipo_analisis} {self.id}'


class EvaluacionFMEA(BaseUnmanagedModel):
    fmea = models.ForeignKey(FMEA_FMECA, on_delete=models.DO_NOTHING, db_column='fmea_id', related_name='evaluaciones')
    estrategia_dimension = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', related_name='evaluaciones_fmea')
    valor_numerico = models.IntegerField(blank=True, null=True)
    valor_texto = models.CharField(max_length=255, blank=True)
    catalogo_fila = models.ForeignKey('DimensionCatalogoFila', on_delete=models.DO_NOTHING, db_column='catalogo_fila_id', blank=True, null=True, related_name='evaluaciones_fmea')
    escala_valor = models.ForeignKey('EscalaValor', on_delete=models.DO_NOTHING, db_column='escala_valor_id', blank=True, null=True, related_name='evaluaciones_fmea')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'evaluacionfmea'
        ordering = ['fmea_id', 'estrategia_dimension_id']
        constraints = [
            models.UniqueConstraint(fields=['fmea', 'estrategia_dimension'], name='uniq_fmea_dimension'),
        ]
        indexes = [
            models.Index(fields=['fmea'], name='idx_evalfmea_fmea'),
            models.Index(fields=['estrategia_dimension'], name='idx_evalfmea_edim'),
        ]

    def __str__(self):
        return f'FMEA {self.fmea_id} / {self.estrategia_dimension}'

    @property
    def valor_display(self):
        if self.valor_numerico is not None:
            return self.valor_numerico
        return self.valor_texto


class TipoTareaEstrategia(BaseUnmanagedModel):
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='tipos_tarea')
    nombre = models.CharField(max_length=150)
    codigo = models.SlugField(max_length=80)
    orden = models.PositiveIntegerField(default=1)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'tipo_tarea_estrategia'
        ordering = ['estrategia_id', 'orden', 'nombre']
        constraints = [
            models.UniqueConstraint(fields=['estrategia', 'codigo'], name='uniq_tte_estrategia_codigo'),
        ]
        indexes = [
            models.Index(fields=['estrategia'], name='idx_tte_estrategia'),
            models.Index(fields=['codigo'], name='idx_tte_codigo'),
            models.Index(fields=['activo'], name='idx_tte_activo'),
        ]

    def __str__(self):
        return f'{self.nombre}'


class CampoTareaEstrategia(BaseUnmanagedModel):
    TIPO_TEXTO = 'texto'
    TIPO_TEXTO_LARGO = 'texto_largo'
    TIPO_NUMERO = 'numero'
    TIPO_DECIMAL = 'decimal'
    TIPO_FECHA = 'fecha'
    TIPO_BOOLEANO = 'booleano'
    TIPO_OPCION = 'opcion'
    TIPO_DATO_CHOICES = [
        (TIPO_TEXTO, 'Texto'),
        (TIPO_TEXTO_LARGO, 'Texto largo'),
        (TIPO_NUMERO, 'Número entero'),
        (TIPO_DECIMAL, 'Número decimal'),
        (TIPO_FECHA, 'Fecha'),
        (TIPO_BOOLEANO, 'Sí/No'),
        (TIPO_OPCION, 'Opción'),
    ]
    tipo_tarea_estrategia = models.ForeignKey(TipoTareaEstrategia, on_delete=models.DO_NOTHING, db_column='tipo_tarea_estrategia_id', related_name='campos')
    nombre = models.CharField(max_length=150)
    clave = models.SlugField(max_length=100)
    tipo_dato = models.CharField(max_length=20, choices=TIPO_DATO_CHOICES, default=TIPO_TEXTO)
    opciones_json = models.TextField(blank=True)
    obligatorio = models.BooleanField(default=False)
    orden = models.PositiveIntegerField(default=1)
    activo = models.BooleanField(default=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'campo_tarea_estrategia'
        ordering = ['tipo_tarea_estrategia_id', 'orden', 'nombre']
        constraints = [
            models.UniqueConstraint(fields=['tipo_tarea_estrategia', 'clave'], name='uniq_cte_tipo_clave'),
        ]
        indexes = [
            models.Index(fields=['tipo_tarea_estrategia'], name='idx_cte_tipo_tarea'),
            models.Index(fields=['clave'], name='idx_cte_clave'),
            models.Index(fields=['activo'], name='idx_cte_activo'),
        ]

    def __str__(self):
        return f'{self.tipo_tarea_estrategia} / {self.nombre}'


class TareaRCM(BaseUnmanagedModel):
    ESTADO_ACTIVO = 'activo'
    ESTADO_INACTIVO = 'inactivo'
    ESTADO_REEMPLAZADO = 'reemplazado'
    ESTADO_ELIMINADO = 'eliminado'
    ESTADO_CHOICES = [
        (ESTADO_ACTIVO, 'Activo'),
        (ESTADO_INACTIVO, 'Inactivo'),
        (ESTADO_REEMPLAZADO, 'Reemplazado'),
        (ESTADO_ELIMINADO, 'Eliminado'),
    ]

    fmea = models.ForeignKey(FMEA_FMECA, on_delete=models.DO_NOTHING, db_column='fmea_id', related_name='tareas_rcm')
    tipo_tarea_estrategia = models.ForeignKey(TipoTareaEstrategia, on_delete=models.DO_NOTHING, db_column='tipo_tarea_estrategia_id', related_name='tareas_rcm')
    descripcion = models.TextField()
    tactica = models.CharField(max_length=100, blank=True)
    limite_aceptable = models.TextField(blank=True)
    parametros = models.TextField(blank=True)
    riesgo_material = models.TextField(blank=True)
    especialidad = models.CharField(max_length=100, blank=True)
    puesto_trabajo = models.CharField(max_length=100, blank=True)
    estado_equipo = models.CharField(max_length=100, blank=True)
    frecuencia_valor = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    frecuencia_unidad = models.CharField(max_length=50, blank=True)
    frecuencia_texto = models.CharField(max_length=150, blank=True)
    duracion_min = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    duracion_hr = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    cantidad_personas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    hh = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    plan_sap = models.CharField(max_length=100, blank=True)
    descripcion_plan = models.TextField(blank=True)
    hoja_ruta = models.CharField(max_length=100, blank=True)
    texto_hoja_ruta = models.TextField(blank=True)
    operacion_hoja_ruta = models.CharField(max_length=100, blank=True)
    texto_operacion = models.TextField(blank=True)
    operacion_pauta = models.CharField(max_length=100, blank=True)
    pauta = models.CharField(max_length=150, blank=True)
    titulo_pauta = models.TextField(blank=True)
    repuesto = models.TextField(blank=True)
    componente_involucrado = models.TextField(blank=True)
    numero_parte = models.CharField(max_length=100, blank=True)
    numero_sap = models.CharField(max_length=100, blank=True)
    procedimiento_trabajo = models.TextField(blank=True)
    costo_hh = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    costo_repuestos = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    tarifa_servicios = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    costo_total = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    oportunidad_mejora = models.TextField(blank=True)
    orden = models.PositiveIntegerField(default=1)
    estado = models.CharField(max_length=30, choices=ESTADO_CHOICES, default=ESTADO_ACTIVO)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'tarea_rcm'
        ordering = ['fmea_id', 'orden', 'id']
        indexes = [
            models.Index(fields=['fmea'], name='idx_trcm_fmea'),
            models.Index(fields=['tipo_tarea_estrategia'], name='idx_trcm_tipo_estrat'),
            models.Index(fields=['estado'], name='idx_trcm_estado'),
            models.Index(fields=['plan_sap'], name='idx_trcm_plan_sap'),
            models.Index(fields=['hoja_ruta'], name='idx_trcm_hoja_ruta'),
        ]

    def __str__(self):
        return f'{self.fmea} / {self.tipo_tarea_estrategia}: {self.descripcion[:60]}'


class ValorCampoTareaRCM(BaseUnmanagedModel):
    tarea = models.ForeignKey(TareaRCM, on_delete=models.DO_NOTHING, db_column='tarea_id', related_name='valores_campos')
    campo = models.ForeignKey(CampoTareaEstrategia, on_delete=models.DO_NOTHING, db_column='campo_id', related_name='valores_tarea')
    valor_texto = models.TextField(blank=True)
    valor_numero = models.DecimalField(max_digits=18, decimal_places=4, blank=True, null=True)
    valor_booleano = models.BooleanField(blank=True, null=True)
    valor_fecha = models.DateField(blank=True, null=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'valor_campo_tarea_rcm'
        ordering = ['tarea_id', 'campo__orden', 'campo_id']
        constraints = [
            models.UniqueConstraint(fields=['tarea', 'campo'], name='uniq_valor_tarea_campo'),
        ]
        indexes = [
            models.Index(fields=['tarea'], name='idx_vctr_tarea'),
            models.Index(fields=['campo'], name='idx_vctr_campo'),
        ]

    @property
    def valor_display(self):
        if self.valor_fecha is not None:
            return self.valor_fecha
        if self.valor_booleano is not None:
            return 'Sí' if self.valor_booleano else 'No'
        if self.valor_numero is not None:
            return self.valor_numero
        return self.valor_texto

    def __str__(self):
        return f'{self.tarea_id} / {self.campo}: {self.valor_display}'


class PlantillaPauta(BaseUnmanagedModel):
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', blank=True, null=True, related_name='plantillas_pauta')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', blank=True, null=True, related_name='plantillas_pauta')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', blank=True, null=True, related_name='plantillas_pauta')
    nombre = models.CharField(max_length=200)
    archivo = models.FileField(upload_to='plantillas_pautas/')
    activa = models.BooleanField(default=True)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'plantillapauta'
        ordering = ['-activa', 'nombre']
        indexes = [
            models.Index(fields=['empresa'], name='idx_ppauta_empresa'),
            models.Index(fields=['servicio'], name='idx_ppauta_servicio'),
            models.Index(fields=['estrategia'], name='idx_ppauta_estrategia'),
            models.Index(fields=['activa'], name='idx_ppauta_activa'),
        ]

    def __str__(self):
        return self.nombre


class MapeoPlantillaPauta(BaseUnmanagedModel):
    plantilla = models.OneToOneField(PlantillaPauta, on_delete=models.DO_NOTHING, db_column='plantilla_id', related_name='mapeo')
    hoja_principal = models.CharField(max_length=120, blank=True)
    config = models.JSONField(default=dict, blank=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'mapeoplantillapauta'
        ordering = ['plantilla__nombre']

    def __str__(self):
        return f'Mapeo {self.plantilla}'


class Pauta(BaseUnmanagedModel):
    ORIGEN_RCM = 'rcm'
    ORIGEN_FMEA = 'fmea'
    ORIGEN_MANUAL = 'manual'
    ORIGEN_CHOICES = [
        (ORIGEN_RCM, 'RCM'),
        (ORIGEN_FMEA, 'FMEA'),
        (ORIGEN_MANUAL, 'Manual'),
    ]
    ESTADO_BORRADOR = 'borrador'
    ESTADO_GENERADA = 'generada'
    ESTADO_REVISADA = 'revisada'
    ESTADO_APROBADA = 'aprobada'
    ESTADO_CHOICES = [
        (ESTADO_BORRADOR, 'Borrador'),
        (ESTADO_GENERADA, 'Generada'),
        (ESTADO_REVISADA, 'Revisada'),
        (ESTADO_APROBADA, 'Aprobada'),
    ]

    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='pautas')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', blank=True, null=True, related_name='pautas')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', blank=True, null=True, related_name='pautas')
    plantilla = models.ForeignKey(PlantillaPauta, on_delete=models.DO_NOTHING, db_column='plantilla_id', blank=True, null=True, related_name='pautas')
    codigo = models.CharField(max_length=80)
    nombre = models.CharField(max_length=200)
    area = models.CharField(max_length=150, blank=True)
    ubicacion_tecnica = models.CharField(max_length=200, blank=True)
    frecuencia = models.CharField(max_length=150, blank=True)
    especialidad = models.CharField(max_length=100, blank=True)
    estado_equipo = models.CharField(max_length=100, blank=True)
    estrategia_mantenimiento = models.CharField(max_length=150, blank=True)
    cantidad_personas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    duracion_horas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    hh_total = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    origen = models.CharField(max_length=20, choices=ORIGEN_CHOICES, default=ORIGEN_RCM)
    estado = models.CharField(max_length=20, choices=ESTADO_CHOICES, default=ESTADO_BORRADOR)
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'pauta'
        ordering = ['-creado_en', 'codigo']
        constraints = [
            models.UniqueConstraint(fields=['servicio', 'codigo'], name='uniq_pauta_servicio_codigo'),
        ]
        indexes = [
            models.Index(fields=['servicio'], name='idx_pauta_servicio'),
            models.Index(fields=['estrategia'], name='idx_pauta_estrategia'),
            models.Index(fields=['equipo'], name='idx_pauta_equipo'),
            models.Index(fields=['estado'], name='idx_pauta_estado'),
            models.Index(fields=['origen'], name='idx_pauta_origen'),
        ]

    def __str__(self):
        return f'{self.codigo} - {self.nombre}'


class PautaTarea(BaseUnmanagedModel):
    TIPO_PRIMARIA = 'primaria'
    TIPO_SECUNDARIA = 'secundaria'
    TIPO_MANUAL = 'manual'
    TIPO_TAREA_CHOICES = [
        (TIPO_PRIMARIA, 'Primaria'),
        (TIPO_SECUNDARIA, 'Secundaria'),
        (TIPO_MANUAL, 'Manual'),
    ]

    pauta = models.ForeignKey(Pauta, on_delete=models.DO_NOTHING, db_column='pauta_id', related_name='tareas')
    orden = models.PositiveIntegerField(default=1)
    componente = models.CharField(max_length=200, blank=True)
    actividad = models.TextField()
    limite_aceptable = models.TextField(blank=True)
    observacion = models.TextField(blank=True)
    tipo_tarea = models.CharField(max_length=20, choices=TIPO_TAREA_CHOICES, default=TIPO_PRIMARIA)
    origen_modelo = models.CharField(max_length=80, blank=True)
    origen_id = models.PositiveIntegerField(blank=True, null=True)
    frecuencia = models.CharField(max_length=150, blank=True)
    pto_trabajo = models.CharField(max_length=100, blank=True)
    cantidad_personas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    duracion_horas = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    hh = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    estado_equipo = models.CharField(max_length=100, blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'pautatarea'
        ordering = ['pauta_id', 'orden', 'id']
        indexes = [
            models.Index(fields=['pauta'], name='idx_ptarea_pauta'),
            models.Index(fields=['tipo_tarea'], name='idx_ptarea_tipo'),
            models.Index(fields=['origen_modelo', 'origen_id'], name='idx_ptarea_origen'),
        ]

    def __str__(self):
        return f'{self.pauta.codigo} / {self.orden}. {self.actividad[:60]}'


class ReglaGeneracionPauta(BaseUnmanagedModel):
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', blank=True, null=True, related_name='reglas_pauta')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', blank=True, null=True, related_name='reglas_pauta')
    nombre = models.CharField(max_length=150)
    agrupar_por_equipo = models.BooleanField(default=True)
    agrupar_por_ubicacion = models.BooleanField(default=True)
    agrupar_por_frecuencia = models.BooleanField(default=True)
    agrupar_por_especialidad = models.BooleanField(default=True)
    agrupar_por_estado_equipo = models.BooleanField(default=True)
    incluir_tareas_primarias = models.BooleanField(default=True)
    incluir_tareas_secundarias = models.BooleanField(default=False)
    activa = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reglageneracionpauta'
        ordering = ['servicio_id', 'estrategia_id', 'nombre']
        indexes = [
            models.Index(fields=['estrategia'], name='idx_rgpa_estrategia'),
            models.Index(fields=['servicio'], name='idx_rgpa_servicio'),
            models.Index(fields=['activa'], name='idx_rgpa_activa'),
        ]

    def __str__(self):
        return self.nombre


class DimensionCatalogo(BaseUnmanagedModel):
    TIPO_CHOICES = [
        ('opciones', 'Opciones'),
        ('rangos', 'Rangos'),
        ('numerico_libre', 'Numérico libre'),
    ]

    nombre = models.CharField(max_length=200)
    campo = models.CharField(max_length=120)
    tipo = models.CharField(max_length=20)
    descripcion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    estrategia_dimension = models.OneToOneField(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', related_name='catalogo')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'dimensioncatalogo'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class DimensionCatalogoFila(BaseUnmanagedModel):
    etiqueta = models.CharField(max_length=255)
    orden = models.PositiveIntegerField()
    catalogo = models.ForeignKey(DimensionCatalogo, on_delete=models.DO_NOTHING, db_column='catalogo_id', related_name='filas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'dimensioncatalogofila'
        ordering = ['catalogo_id', 'orden']

    def __str__(self):
        return self.etiqueta

    def values_map(self):
        result = {}
        for celda in self.celdas.select_related('columna').all():
            result[celda.columna.clave_interna] = celda.python_value
        return result


class EscalaUnificada(BaseUnmanagedModel):
    nivel = models.PositiveIntegerField(unique=True)
    significado = models.CharField(max_length=100)
    interpretacion = models.CharField(max_length=255)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'escalaunificada'
        ordering = ['nivel']

    def __str__(self):
        return f'{self.nivel} - {self.significado}'


class EscalaValor(BaseUnmanagedModel):
    nivel_ordinal = models.PositiveIntegerField()
    valor_numerico = models.DecimalField(max_digits=10, decimal_places=2)
    codigo = models.CharField(max_length=50)
    descripcion = models.CharField(max_length=255)
    color = models.CharField(max_length=20)
    escala_unificada = models.ForeignKey(EscalaUnificada, on_delete=models.DO_NOTHING, db_column='escala_unificada_id', blank=True, null=True, related_name='valores')
    estrategia_dimension = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', related_name='escalas_valor')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'escalavalor'
        ordering = ['estrategia_dimension_id', 'nivel_ordinal']

    def __str__(self):
        return f'{self.codigo} ({self.valor_numerico})'


class CriticidadDimension(BaseUnmanagedModel):
    valor_numerico = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    valor_booleano = models.BooleanField(blank=True, null=True)
    valor_texto = models.TextField(blank=True)
    comentario = models.TextField(blank=True, verbose_name='Comentario')
    valor_secundario = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    criticidad = models.ForeignKey(Criticidad, on_delete=models.DO_NOTHING, db_column='criticidad_id', related_name='dimensiones')
    dimension = models.ForeignKey(Dimension, on_delete=models.DO_NOTHING, db_column='dimension_id', related_name='criticidades_dimension')
    catalogo_fila = models.ForeignKey(DimensionCatalogoFila, on_delete=models.DO_NOTHING, db_column='catalogo_fila_id', blank=True, null=True, related_name='criticidades')
    escala_unificada = models.ForeignKey(EscalaUnificada, on_delete=models.DO_NOTHING, db_column='escala_unificada_id', blank=True, null=True, related_name='criticidades')
    escala_valor = models.ForeignKey(EscalaValor, on_delete=models.DO_NOTHING, db_column='escala_valor_id', blank=True, null=True, related_name='criticidades')
    estrategia_dimension = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', blank=True, null=True, related_name='criticidades')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'criticidaddimension'
        ordering = ['criticidad_id', 'id']

    def __str__(self):
        return f'Criticidad {self.criticidad_id} / {self.dimension}'


class DimensionCatalogoColumna(BaseUnmanagedModel):
    TIPO_DATO_CHOICES = [
        ('texto', 'Texto'),
        ('numero', 'Número'),
        ('booleano', 'Booleano'),
        ('color', 'Color'),
    ]

    nombre_columna = models.CharField(max_length=150)
    clave_interna = models.CharField(max_length=100)
    tipo_dato = models.CharField(max_length=20)
    orden = models.PositiveIntegerField()
    visible_en_registro = models.BooleanField('Visible en registro', default=True)
    catalogo = models.ForeignKey(DimensionCatalogo, on_delete=models.DO_NOTHING, db_column='catalogo_id', related_name='columnas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'dimensioncatalogocolumna'
        ordering = ['catalogo_id', 'orden']

    def __str__(self):
        return self.nombre_columna


class DimensionCatalogoCelda(BaseUnmanagedModel):
    valor_texto = models.TextField(blank=True)
    valor_numero = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    valor_booleano = models.BooleanField(blank=True, null=True)
    columna = models.ForeignKey(DimensionCatalogoColumna, on_delete=models.DO_NOTHING, db_column='columna_id', related_name='celdas')
    fila = models.ForeignKey(DimensionCatalogoFila, on_delete=models.DO_NOTHING, db_column='fila_id', related_name='celdas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'dimensioncatalogocelda'
        ordering = ['fila_id', 'columna_id']

    def __str__(self):
        return f'Celda {self.id}'

    @property
    def python_value(self):
        if self.valor_booleano is not None:
            return self.valor_booleano
        if self.valor_numero is not None:
            return self.valor_numero
        return self.valor_texto


class InicioSesion(BaseUnmanagedModel):
    hora = models.DateTimeField()
    longitud = models.DecimalField(max_digits=9, decimal_places=6)
    latitud = models.DecimalField(max_digits=9, decimal_places=6)
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', related_name='inicios_sesion')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'iniciosesion'
        ordering = ['-hora']

    def __str__(self):
        return f'{self.usuario} - {self.hora}'


class MatrizRiesgo(BaseUnmanagedModel):
    MODO_MANUAL = 'manual'
    MODO_AUTOMATICA_MAXIMO_TEORICO = 'automatica_maximo_teorico'
    RESOLUCION_EXACTA = 'exacta'
    RESOLUCION_UMBRAL_RESULTADO = 'umbral_resultado'
    RESOLUCION_CHOICES = [
        (RESOLUCION_EXACTA, 'Coincidencia exacta por ejes'),
        (RESOLUCION_UMBRAL_RESULTADO, 'Umbral inferior por resultado'),
    ]
    EJE_HORIZONTAL_CHOICES = [
        ('impacto', 'Consecuencia'),
        ('probabilidad', 'Probabilidad'),
    ]

    nombre = models.CharField(max_length=200)
    fecha_creado = models.DateField()
    eje_horizontal = models.CharField(max_length=20)
    leyenda_json = models.TextField(blank=True)
    dimension_impacto = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='dimension_impacto_id', blank=True, null=True, related_name='matrices_impacto')
    dimension_probabilidad = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='dimension_probabilidad_id', blank=True, null=True, related_name='matrices_probabilidad')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='matrices_riesgo')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'matrizriesgo'
        ordering = ['-fecha_creado', 'nombre']

    def __str__(self):
        return self.nombre


class NivelImpacto(BaseUnmanagedModel):
    nombre = models.CharField(max_length=150)
    valor = models.DecimalField(max_digits=10, decimal_places=2)
    descripcion = models.CharField(max_length=255)
    orden_visual = models.PositiveIntegerField()
    matriz = models.ForeignKey(MatrizRiesgo, on_delete=models.DO_NOTHING, db_column='matriz_id', related_name='niveles_impacto')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'nivelesejey'
        ordering = ['matriz_id', 'orden_visual']

    def __str__(self):
        return f'{self.nombre} ({self.valor})'


class NivelProbabilidad(BaseUnmanagedModel):
    nombre = models.CharField(max_length=150)
    valor = models.DecimalField(max_digits=10, decimal_places=2)
    descripcion = models.CharField(max_length=255)
    orden_visual = models.PositiveIntegerField()
    matriz = models.ForeignKey(MatrizRiesgo, on_delete=models.DO_NOTHING, db_column='matriz_id', related_name='niveles_probabilidad')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'nivelesejex'
        ordering = ['matriz_id', 'orden_visual']

    def __str__(self):
        return f'{self.nombre} ({self.valor})'


class MatrizRiesgoCelda(BaseUnmanagedModel):
    clasificacion = models.CharField(max_length=100)
    color = models.CharField(max_length=20)
    resultado_num = models.DecimalField(max_digits=10, decimal_places=2)
    calcular = models.BooleanField(default=True)
    matriz = models.ForeignKey(MatrizRiesgo, on_delete=models.DO_NOTHING, db_column='matriz_id', related_name='celdas')
    impacto_nivel = models.ForeignKey(NivelImpacto, on_delete=models.DO_NOTHING, db_column='impacto_nivel_id', related_name='celdas')
    probabilidad = models.ForeignKey(NivelProbabilidad, on_delete=models.DO_NOTHING, db_column='probabilidad_id', related_name='celdas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'matrizriesgocelda'
        ordering = ['matriz_id', 'id']

    def __str__(self):
        return f'{self.matriz} / {self.probabilidad} x {self.impacto_nivel}'
