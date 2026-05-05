from django.contrib.auth.models import User
from django.db import models


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
        db_table = 'reliability_empresa'
        ordering = ['nombre']

    def __str__(self):
        return f'{self.nombre} ({self.sigla})'


class Metodologia(BaseUnmanagedModel):
    nombre = models.CharField(max_length=200)
    abreviatura = models.CharField(max_length=20)
    descripcion = models.TextField(blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_metodologia'
        ordering = ['nombre']

    def __str__(self):
        return f'{self.abreviatura} - {self.nombre}'


class Cargo(BaseUnmanagedModel):
    nombre_cargo = models.CharField(max_length=150)
    area = models.CharField(max_length=150)
    jefatura = models.CharField(max_length=150)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_cargo'
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
        db_table = 'reliability_estrategia'
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
        db_table = 'reliability_usuario'
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
        db_table = 'reliability_usuario_eliminado'
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
    creado_por_usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='creado_por_usuario_id', blank=True, null=True, related_name='servicios_creados')
    responsable_usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='responsable_usuario_id', blank=True, null=True, related_name='servicios_responsables')
    metodologias = models.ManyToManyField(Metodologia, through='ServicioMetodologia', related_name='servicios', blank=True,)
    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_servicio'
        ordering = ['-creado_en', 'codigo_servicio']

    def __str__(self):
        return self.codigo_servicio

class ServicioMetodologia(BaseUnmanagedModel):
    servicio = models.ForeignKey(
        Servicio,
        on_delete=models.DO_NOTHING,
        db_column='servicio_id',
        related_name='servicio_metodologias',
    )
    metodologia = models.ForeignKey(
        Metodologia,
        on_delete=models.DO_NOTHING,
        db_column='metodologia_id',
        related_name='servicio_metodologias',
    )

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_serviciometodologia'
        ordering = ['servicio_id', 'metodologia_id']

    def __str__(self):
        return f'{self.servicio} / {self.metodologia}'

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
        db_table = 'reliability_accesousuario'
        ordering = ['-creado_en']

    def __str__(self):
        scope = self.servicio or self.estrategia or self.empresa
        return f'{self.usuario} -> {scope}'


class Componente(BaseUnmanagedModel):
    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_componente'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class Sistema(BaseUnmanagedModel):
    nombre_sistema = models.CharField(max_length=200)
    codigo_sistema = models.CharField(max_length=100)
    empresa = models.ForeignKey(Empresa, on_delete=models.DO_NOTHING, db_column='empresa_id', related_name='sistemas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_sistemas'
        ordering = ['empresa__nombre', 'nombre_sistema']

    def __str__(self):
        return f'{self.codigo_sistema} - {self.nombre_sistema}'


class Equipo(BaseUnmanagedModel):
    tag_equipo = models.CharField(max_length=100)
    nombre_equipo = models.CharField(max_length=200)
    ut = models.CharField(max_length=200)
    descripcion_ut = models.CharField(max_length=255)
    otros_posibles = models.TextField(blank=True)
    sistema = models.ForeignKey(Sistema, on_delete=models.DO_NOTHING, db_column='sistema_id', related_name='equipos')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_equipo'
        ordering = ['tag_equipo', 'nombre_equipo']

    def __str__(self):
        return f'{self.tag_equipo} - {self.nombre_equipo}'


class ComponenteEquipo(BaseUnmanagedModel):
    componente = models.ForeignKey(Componente, on_delete=models.DO_NOTHING, db_column='componente_id', related_name='componentes_equipo')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='componentes')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_componenteequipo'
        ordering = ['equipo_id', 'componente_id']

    def __str__(self):
        return f'{self.equipo} / {self.componente}'


class ServicioEquipo(BaseUnmanagedModel):
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='servicios_equipo')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='equipos_servicio')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_servicioequipo'
        ordering = ['servicio_id', 'equipo_id']

    def __str__(self):
        return f'{self.servicio} / {self.equipo}'


class AcaCarga(BaseUnmanagedModel):
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
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='cargas_aca')
    servicio = models.ForeignKey(Servicio, on_delete=models.DO_NOTHING, db_column='servicio_id', related_name='cargas_aca')
    usuario = models.ForeignKey(Usuario, on_delete=models.DO_NOTHING, db_column='usuario_id', blank=True, null=True, related_name='cargas_aca')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_acacarga'
        ordering = ['-fecha_analisis', '-creado_en']

    def __str__(self):
        return f'ACA {self.id} - {self.fecha_analisis}'


class Criticidad(BaseUnmanagedModel):
    escenario_falla = models.TextField(blank=True)
    frecuencia_original = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    frecuencia_normalizada = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    valor_cons_total = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    indicador_criticidad = models.CharField(max_length=100)
    valor_criticidad_equipo = models.DecimalField(max_digits=10, decimal_places=2, blank=True, null=True)
    criticidad_final = models.CharField(max_length=30)
    creado_en = models.DateTimeField()
    aca_carga = models.ForeignKey(AcaCarga, on_delete=models.DO_NOTHING, db_column='aca_carga_id', related_name='criticidades')
    equipo = models.ForeignKey(Equipo, on_delete=models.DO_NOTHING, db_column='equipo_id', related_name='criticidades')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_criticidad'
        ordering = ['-creado_en', 'id']

    def __str__(self):
        return f'Criticidad {self.id} - {self.equipo}'


class Dimension(BaseUnmanagedModel):
    TIPO_FUNCIONAL_CHOICES = [
        ('impacto', 'Impacto'),
        ('probabilidad', 'Probabilidad'),
        ('resultado', 'Resultado'),
        ('catalogo', 'Catálogo'),
        ('atributo', 'Atributo'),
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
    ]

    nombre = models.CharField(max_length=200)
    descripcion = models.TextField(blank=True)
    tipo_funcional = models.CharField(max_length=20)
    tipo_dato = models.CharField(max_length=20)
    tipo_calculo = models.CharField(max_length=30, blank=True, null=True)
    config_calculo = models.TextField(blank=True, null=True)

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_dimension'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class EstrategiaDimension(BaseUnmanagedModel):
    orden = models.PositiveIntegerField()
    obligatorio = models.BooleanField(default=False)
    activo = models.BooleanField(default=True)
    dimension = models.ForeignKey(Dimension, on_delete=models.DO_NOTHING, db_column='dimension_id', related_name='estrategias_dimension')
    estrategia = models.ForeignKey(Estrategia, on_delete=models.DO_NOTHING, db_column='estrategia_id', related_name='dimensiones_estrategia')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_estrategiadimension'
        ordering = ['estrategia_id', 'orden']

    def __str__(self):
        return f'{self.estrategia} / {self.dimension}'


class DimensionCatalogo(BaseUnmanagedModel):
    TIPO_CHOICES = [
        ('opciones', 'Opciones'),
        ('rangos', 'Rangos'),
    ]

    nombre = models.CharField(max_length=200)
    campo = models.CharField(max_length=120)
    tipo = models.CharField(max_length=20)
    descripcion = models.TextField(blank=True)
    activa = models.BooleanField(default=True)
    estrategia_dimension = models.OneToOneField(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', related_name='catalogo')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_dimensioncatalogo'
        ordering = ['nombre']

    def __str__(self):
        return self.nombre


class DimensionCatalogoFila(BaseUnmanagedModel):
    etiqueta = models.CharField(max_length=255)
    orden = models.PositiveIntegerField()
    catalogo = models.ForeignKey(DimensionCatalogo, on_delete=models.DO_NOTHING, db_column='catalogo_id', related_name='filas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_dimensioncatalogofila'
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
        db_table = 'reliability_escalaunificada'
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
        db_table = 'reliability_escalavalor'
        ordering = ['estrategia_dimension_id', 'nivel_ordinal']

    def __str__(self):
        return f'{self.codigo} ({self.valor_numerico})'


class CriticidadDimension(BaseUnmanagedModel):
    valor_numerico = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    valor_booleano = models.BooleanField(blank=True, null=True)
    valor_texto = models.TextField(blank=True)
    valor_secundario = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    criticidad = models.ForeignKey(Criticidad, on_delete=models.DO_NOTHING, db_column='criticidad_id', related_name='dimensiones')
    dimension = models.ForeignKey(Dimension, on_delete=models.DO_NOTHING, db_column='dimension_id', related_name='criticidades_dimension')
    catalogo_fila = models.ForeignKey(DimensionCatalogoFila, on_delete=models.DO_NOTHING, db_column='catalogo_fila_id', blank=True, null=True, related_name='criticidades')
    escala_unificada = models.ForeignKey(EscalaUnificada, on_delete=models.DO_NOTHING, db_column='escala_unificada_id', blank=True, null=True, related_name='criticidades')
    escala_valor = models.ForeignKey(EscalaValor, on_delete=models.DO_NOTHING, db_column='escala_valor_id', blank=True, null=True, related_name='criticidades')
    estrategia_dimension = models.ForeignKey(EstrategiaDimension, on_delete=models.DO_NOTHING, db_column='estrategia_dimension_id', blank=True, null=True, related_name='criticidades')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_criticidaddimension'
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
    catalogo = models.ForeignKey(DimensionCatalogo, on_delete=models.DO_NOTHING, db_column='catalogo_id', related_name='columnas')

    class Meta(BaseUnmanagedModel.Meta):
        db_table = 'reliability_dimensioncatalogocolumna'
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
        db_table = 'reliability_dimensioncatalogocelda'
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
        db_table = 'reliability_iniciosesion'
        ordering = ['-hora']

    def __str__(self):
        return f'{self.usuario} - {self.hora}'


class MatrizRiesgo(BaseUnmanagedModel):
    EJE_HORIZONTAL_CHOICES = [
        ('impacto', 'Impacto'),
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
        db_table = 'reliability_matrizriesgo'
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
        db_table = 'reliability_nivelesimpacto'
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
        db_table = 'reliability_nivelesprobabilidad'
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
        db_table = 'reliability_matrizriesgocelda'
        ordering = ['matriz_id', 'id']

    def __str__(self):
        return f'{self.matriz} / {self.probabilidad} x {self.impacto_nivel}'
