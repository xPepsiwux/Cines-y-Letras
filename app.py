import os
from functools import wraps
from flask import Flask, render_template, redirect, url_for, flash, request, session, send_from_directory, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, login_required, logout_user, current_user, UserMixin
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, SelectField, TextAreaField, FloatField
from wtforms.validators import DataRequired, Length, EqualTo
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import func
from groq import Groq # 🌟 Importamos el cliente de Groq
import json
import random

# ---------- CONFIGURACIÓN BÁSICA ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(INSTANCE_DIR, exist_ok=True)

ALLOWED_EXT = {'png', 'jpg', 'jpeg', 'gif'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXT

app = Flask(__name__)
app.config['SECRET_KEY'] = 'dev_secret_key_change_me'
csrf = CSRFProtect(app)

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(INSTANCE_DIR, 'cine_letras.sqlite')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# ---------- MODELOS ----------

class Follow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    follower_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    followed_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user')

    # RELACIONES PARA CONTADORES (Corregido para sincronización)
    followers = db.relationship('Follow', foreign_keys=[Follow.followed_id], backref='followed_user', lazy='dynamic')
    following = db.relationship('Follow', foreign_keys=[Follow.follower_id], backref='follower_user', lazy='dynamic')

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, plain):
        return check_password_hash(self.password_hash, plain)

    def is_admin(self):
        return self.role == 'admin'

    def is_moderator(self):
        return self.role in ['moderator', 'admin']

    def is_following(self, user):
        return Follow.query.filter_by(
            follower_id=self.id,
            followed_id=user.id
        ).first() is not None

class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(250), nullable=False)
    type = db.Column(db.String(30), nullable=False)
    genre = db.Column(db.String(80))
    rating = db.Column(db.Float)
    comment = db.Column(db.Text)
    image = db.Column(db.String(300))
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='items')

# ---------- FORMULARIOS ----------
class LoginForm(FlaskForm):
    username = StringField('Nombre de usuario', validators=[DataRequired()])
    password = PasswordField('Contraseña', validators=[DataRequired()])
    submit = SubmitField('Iniciar sesión')

class RegisterForm(FlaskForm):
    username = StringField('Nombre de usuario', validators=[DataRequired(), Length(min=3, max=80)])
    password = PasswordField('Contraseña', validators=[DataRequired(), Length(min=4)])
    confirm = PasswordField('Confirmar contraseña', validators=[DataRequired(), EqualTo('password')])
    submit = SubmitField('Registrarse')

class ItemForm(FlaskForm):
    title = StringField('Título', validators=[DataRequired()])
    type = SelectField('Tipo', choices=[('libro','Libro'),('pelicula','Película'),('serie','Serie')], validators=[DataRequired()])
    genre = SelectField('Género', choices=[('accion','Acción'),('aventura','Aventura'),('romance','Romance'),('terror','Terror'),('comedia','Comedia'),('drama','Drama'),('fantasia','Fantasía'),('ciencia_ficcion','Ciencia Ficción'),('misterio','Misterio'),('documental','Documental')])
    rating = FloatField('Calificación (1 – 10)')
    comment = TextAreaField('Comentario')
    submit = SubmitField('Guardar')

# ---------- LOGIN ----------
@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ---------- CONTEXT ROLES ----------
@app.context_processor
def inject_roles():
    return dict(
        is_admin=current_user.is_authenticated and current_user.is_admin(),
        is_moderator=current_user.is_authenticated and current_user.is_moderator()
    )

# ---------- DECORADORES ----------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function

def moderator_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_moderator():
            abort(403)
        return f(*args, **kwargs)
    return decorated_function


# ===================================================
# 🧠 CONFIGURACIÓN DEL CEREBRO DE LUMI (GROQ SEGURO)
# ===================================================

api_key_groq = os.environ.get("GROQ_API_KEY", "")

# Inicializamos el cliente con la variable protegida
client_groq = Groq(api_key=api_key_groq)

def preguntar_a_ia(mensaje_usuario):
    """Envía el texto al modelo de Groq manteniendo el rol de Lumi"""
    try:
        system_instruction = (
            f"Eres Lumi, la IA asistente oficial y alma de la plataforma 'Cine & Letras'. "
            f"Estás teniendo una conversación libre con el usuario {current_user.username}. "
            f"Tu personalidad es alegre, cinéfila, carismática y sumamente apasionada por los libros, las películas y las series. "
            f"Exprésate de forma cercana, usando emojis acordes a la charla (🍿, 🎬, 📚, ✨, 😮). "
            f"Mantén tus respuestas relativamente concisas (máximo 2 o 3 párrafos cortos) para que quepan bien en el chat flotante. "
            f"Importante: Como eres una IA autónoma, si te piden opiniones sobre obras o recomendaciones generales fuera de los comandos fijos, "
            f"responde usando tu propio criterio con total libertad y naturalidad."
        )

       
        completion = client_groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": mensaje_usuario}
            ],
            temperature=0.7
        )
        
        return completion.choices[0].message.content

    except Exception as e:
        print(f"⚠️ Error en la conexión con la IA: {e}")
        return "✨ Vaya, mi mente se quedó colgada analizando el final de una película... ¿Podrías repetirme eso?"

def responder_lumi(mensaje):
    mensaje = mensaje.lower().strip()

    # ===================================================
    # 📦 BANCO DE RECOMENDACIONES POR GÉNERO (FASE 4)
    # ===================================================
    banco_recomendaciones = {
        "accion": {
            "pelicula": ["John Wick: Capítulo 4", "Mad Max: Fury Road", "Gladiador"],
            "serie": ["The Boys", "Daredevil", "Reacher"],
            "libro": ["Giro inesperado", "La lista terminal", "Noche de fuego"]
        },
        "ciencia_ficcion": {
            "pelicula": ["The Martian", "Ex Machina", "Contact", "2001: Odisea del Espacio"],
            "serie": ["Black Mirror", "Severance", "Westworld"],
            "libro": ["Dune", "Fahrenheit 451", "El problema de los tres cuerpos"]
        },
        "romance": {
            "pelicula": ["Cuestión de tiempo (About Time)", "La La Land", "Antes del amanecer"],
            "serie": ["Normal People", "Bridgerton", "Heartstopper"],
            "libro": ["Orgullo y Prejuicio", "Bajo la misma estrella", "Yo antes de ti"]
        },
        "terror": {
            "pelicula": ["El Conjuro", "Hereditary", "Un lugar en silencio"],
            "serie": ["La maldición de Hill House", "Stranger Things", "Misa de medianoche"],
            "libro": ["It", "Drácula", "El resplandor"]
        },
        "drama": {
            "pelicula": ["The Shawshank Redemption", "Forrest Gump", "Whiplash"],
            "serie": ["Succession", "Better Call Saul", "The Crown"],
            "libro": ["El gran Gatsby", "Matar a un ruiseñor", "Crimen y castigo"]
        },
        "comedia": {
            "pelicula": ["¿Qué pasó ayer? (The Hangover)", "Superbad", "La Máscara"],
            "serie": ["The Office", "Friends", "Brooklyn Nine-Nine"],
            "libro": ["Guía del autoestopista galáctico", "Sin noticias de Gurb"]
        },
        "fantasia": {
            "pelicula": ["El Señor de los Anillos: La Comunidad del Anillo", "Harry Potter y el prisionero de Azkaban", "Crónicas de Narnia"],
            "serie": ["The Witcher", "House of the Dragon", "Shadow and Bone"],
            "libro": ["El Hobbit", "El nombre del viento", "Nacidos de la bruma"]
        },
        "misterio": {
            "pelicula": ["Zodiac", "Se7en", "Knives Out"],
            "serie": ["Sherlock", "Mindhunter", "True Detective"],
            "libro": ["El código Da Vinci", "Estudio en escarlata", "La chica del tren"]
        },
        "aventura": {
            "pelicula": ["Indiana Jones", "Jurassic Park", "Interstellar"],
            "serie": ["The Mandalorian", "One Piece (Live Action)", "Lost"],
            "libro": ["La isla del tesoro", "La vuelta al mundo en 80 días"]
        }
    }

    # ===================================================
    # 🌟 PASO 1, 4 & 5: LISTAS DE PERSONALIDAD Y HUMOR
    # ===================================================
    saludos = [
        "😊 ¡Hola! Soy Lumi. ¿Qué historia descubrirás hoy?",
        "🎬 ¡Qué gusto volver a verte! ¿En qué puedo ayudarte?",
        "📚 Bienvenido nuevamente. ¿Buscas una recomendación o quieres agregar algo?",
        "✨ Hola. Siempre es un buen día para descubrir una nueva historia.",
        "🌟 ¡Hola! Estoy lista para ayudarte."
    ]

    despedidas = [
        "👋 ¡Hasta luego! Espero verte pronto.",
        "📚 Fue un gusto ayudarte. ¡Que disfrutes tu próxima historia!",
        "🎬 Nos vemos muy pronto.",
        "😊 Aquí estaré cuando me necesites."
    ]

    no_entiendo = [
        "🤔 Creo que aún no aprendí eso. ¿Podrías explicarlo de otra forma?",
        "😅 Todavía estoy aprendiendo. Intenta preguntármelo de otra manera.",
        "✨ No estoy segura de entenderte, pero quiero ayudarte."
    ]

    gracias_respuestas = [
        "😊 ¡Siempre es un placer ayudar!",
        "✨ Para eso estoy.",
        "📚 Me alegra haberte ayudado.",
        "🎬 ¡No hay de qué!",
        "😁 Cuando quieras."
    ]

    # ===================================================
    # 🌟 PASO 7 & 6: CONTROL DE SALUDOS Y BIENVENIDA DINÁMICA
    # ===================================================
    if any(x in mensaje for x in ["hola", "buenas", "hey", "holi", "alo"]):
        # Obtener métricas reales de la BD
        total_libros = Item.query.filter_by(user_id=current_user.id, type='libro').count()
        total_peliculas = Item.query.filter_by(user_id=current_user.id, type='pelicula').count()
        total_series = Item.query.filter_by(user_id=current_user.id, type='serie').count()
        total_general = total_libros + total_peliculas + total_series

        # Paso 6: Evaluar si el usuario merece un cumplido por volumen
        cumplido = ""
        if total_general >= 100:
            cumplido = "\n\n👑 ¡Increíble! Eres uno de los mayores coleccionistas de Cine & Letras."
        elif total_general >= 50:
            cumplido = "\n\n🏆 ¡Impresionante! Ya tienes una biblioteca audiovisual enorme."
        elif total_general >= 20:
            cumplido = "\n\n😮 ¡Ya llevas más de 20 contenidos registrados! Tu colección comienza a verse increíble."

        return (
            f"🌟 Hola {current_user.username}.\n\n"
            f"{random.choice(saludos)}\n\n"
            f"Actualmente tienes:\n"
            f"🎬 {total_peliculas} películas\n"
            f"📺 {total_series} series\n"
            f"📚 {total_libros} libros"
            f"{cumplido}\n\n"
            f"¿Qué haremos hoy?"
        )

    # CONTROL DE DESPEDIDAS
    if any(x in mensaje for x in ["adios", "chao", "hasta luego", "bye"]):
        return random.choice(despedidas)

    # PASO 5: DETECCIÓN DE GRATITUD (HUMOR Y DETALLES)
    if any(x in mensaje for x in ["gracias", "thank you", "ty", "agradecido"]):
        return random.choice(gracias_respuestas)

    # ===================================================
    # 📊 SECCIÓN DE CONSULTAS A LA BASE DE DATOS
    # ===================================================
    if "cuántos contenidos" in mensaje or "cuantos contenidos" in mensaje:
        total = Item.query.filter_by(user_id=current_user.id).count()
        return f"📚 Actualmente tienes {total} contenidos registrados en total."

    if "cuántas películas" in mensaje or "cuantas peliculas" in mensaje:
        total = Item.query.filter_by(user_id=current_user.id, type="pelicula").count()
        return f"🎬 Has agregado {total} películas."

    if "cuántos libros" in mensaje or "cuantos libros" in mensaje:
        total = Item.query.filter_by(user_id=current_user.id, type="libro").count()
        return f"📘 Tienes {total} libros registrados."

    if "cuántas series" in mensaje or "cuantas series" in mensaje:
        total = Item.query.filter_by(user_id=current_user.id, type="serie").count()
        return f"📺 Has agregado {total} series."

    if "último" in mensaje or "ultimo" in mensaje:
        ultimo = Item.query.filter_by(user_id=current_user.id).order_by(Item.id.desc()).first()
        if ultimo:
            return f"🆕 Tu último contenido agregado fue **{ultimo.title}**."
        return "Todavía no has agregado ningún contenido a tu colección."

    if "mejor calificación" in mensaje or "mejor puntuación" in mensaje:
        mejor = Item.query.filter(Item.user_id == current_user.id, Item.rating != None).order_by(Item.rating.desc()).first()
        if mejor:
            return f"⭐ Tu mejor calificación es un **{mejor.rating}/10** para *{mejor.title}*."
        return "Todavía no tienes contenidos calificados."

    if "peor calificación" in mensaje or "peor puntuación" in mensaje:
        peor = Item.query.filter(Item.user_id == current_user.id, Item.rating != None).order_by(Item.rating.asc()).first()
        if peor:
            return f"⭐ Tu calificación más baja es un **{peor.rating}/10** para *{peor.title}*."
        return "Todavía no tienes contenidos calificados."

    if "promedio" in mensaje:
        promedio = db.session.query(func.avg(Item.rating)).filter(Item.user_id == current_user.id).scalar()
        if promedio:
            return f"📊 Tu promedio de calificaciones global es de **{round(promedio, 2)}/10**."
        return "Aún no tienes suficientes notas para calcular un promedio."

    if "género favorito" in mensaje or "genero favorito" in mensaje:
        favorito = db.session.query(
            Item.genre,
            func.count(Item.genre)
        ).filter(
            Item.user_id == current_user.id,
            Item.genre != None,
            Item.genre != "Sin especificar"
        ).group_by(Item.genre).order_by(func.count(Item.genre).desc()).first()

        if favorito and len(favorito) > 0 and favorito[0]:
            return f"🎭 Tu género favorito en las listas es **{favorito[0].capitalize()}**."
        return "Todavía no poseo registros suficientes para deducir tu género favorito."

    # ===================================================
    # 👥 FASE 2: LUMI CONOCE LA COMUNIDAD
    # ===================================================
    if "quién tiene más" in mensaje or "quien tiene mas" in mensaje:
        top_usuario = db.session.query(
            User.username, 
            func.count(Item.id).label('total')
        ).join(Item, User.id == Item.user_id).group_by(User.id).order_by(func.count(Item.id).desc()).first()

        if top_usuario:
            return f"🏆 El usuario **{top_usuario.username}** posee la mayor cantidad de contenidos con un total de **{top_usuario.total}** registros."
        return "Aún no hay suficientes datos en la comunidad."

    if "usuarios que vean anime" in mensaje or "quien ve anime" in mensaje:
        usuarios_anime = db.session.query(User.username).distinct().join(Item, User.id == Item.user_id)\
            .filter((Item.type == "anime") | (Item.genre == "anime")).filter(User.id != current_user.id).all()

        if usuarios_anime:
            lista = "<br>• " + "<br>• ".join([u.username for u in usuarios_anime])
            return f"🍿 Encontré estos usuarios que disfrutan del anime:{lista}"
        return "Por los momentos ningún otro usuario ha registrado anime."

    if any(x in mensaje for x in ["recomiéndame alguien", "recomiendame alguien", "a quién seguir", "a quien seguir"]):
        sugerido = db.session.query(
            User.username, 
            func.count(Item.id).label('total')
        ).filter(User.id != current_user.id).join(Item, User.id == Item.user_id).group_by(User.id).order_by(func.count(Item.id).desc()).first()

        if sugerido:
            return f"✨ Te recomiendo echarle un ojo al perfil de **{sugerido.username}**. Tiene {sugerido.total} contenidos guardados y su lista se mueve bastante."
        return "No encontré otros usuarios disponibles en la comunidad para recomendarte hoy."

    # ===================================================
    # 🧠 FASE 4: RECOMENDACIONES INTELIGENTES INTERNAS
    # ===================================================
    pide_pelicula = "película" in mensaje or "pelicula" in mensaje
    pide_serie = "serie" in mensaje
    pide_libro = "libro" in mensaje

    if pide_pelicula or pide_serie or pide_libro:
        tipo_solicitado = "pelicula" if pide_pelicula else "serie" if pide_serie else "libro"
        
        gusto_predominante = db.session.query(
            Item.genre,
            func.count(Item.genre)
        ).filter(
            Item.user_id == current_user.id,
            Item.rating >= 7.0,
            Item.genre != None,
            Item.genre != "Sin especificar"
        ).group_by(Item.genre).order_by(func.count(Item.genre).desc()).first()

        if gusto_predominante and gusto_predominante[0]:
            genero_deducido = gusto_predominante[0]
            opciones = banco_recomendaciones.get(genero_deducido, {}).get(tipo_solicitado, [])
            
            if opciones:
                recomendacion = random.choice(opciones)
                genero_bonito = genero_deducido.replace("_", " ").capitalize()
                return (
                    f"🧐 Analizando tus listas, veo que te encantan los contenidos de **{genero_bonito}** (los tienes con las notas más altas).\n\n"
                    f"✨ Basado en eso, te recomiendo leer/ver: **{recomendacion}**."
                )

        genero_azar = random.choice(list(banco_recomendaciones.keys()))
        opciones_azar = banco_recomendaciones[genero_azar][tipo_solicitado]
        recomendacion_azar = random.choice(opciones_azar)
        genero_bonito_azar = genero_azar.replace("_", " ").capitalize()
        
        return (
            f"Como aún no tengo suficientes contenidos con notas altas en tu perfil para armar un patrón de gustos, elegí algo genial al azar.\n\n"
            f"🍿 ¿Qué tal un toque de **{genero_bonito_azar}**? Te sugiero: **{recomendacion_azar}**."
        )

    # PALABRAS CLAVE UNITARIAS
    if "acción" in mensaje or "accion" in mensaje:
        return "💥 Si buscas acción pura, te recomiendo ver **John Wick**."
    if "terror" in mensaje:
        return "👻 Si quieres pasar un buen susto, mírate **El Conjuro**."
    if "romance" in mensaje:
        return "❤️ En el romance clásico, una excelente opción es el libro **Orgullo y Prejuicio**."
    if "comedia" in mensaje:
        return "😂 Si quieres reírte un buen rato, mírate **¿Qué pasó ayer?**."
    
    respuesta_ia = preguntar_a_ia(mensaje)
    return respuesta_ia if respuesta_ia else "🤔 Déjame procesar eso un momento..."


    # ---------- RUTAS ----------
@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash(f'Bienvenido/a, {user.username}', 'success')
            return redirect(url_for('dashboard'))
        flash('Usuario o contraseña incorrectos', 'danger')
    return render_template('login.html', form=form)

@app.route('/register', methods=['GET','POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        if User.query.filter_by(username=form.username.data).first():
            flash('El nombre de usuario ya existe', 'warning')
            return redirect(url_for('register'))
        user = User(username=form.username.data, role='user')
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('Registro exitoso. Ahora puedes iniciar sesión.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Sesión cerrada correctamente', 'info')
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    items = Item.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', items=items)

@app.route('/add', methods=['GET','POST'])
@login_required
def add_item():
    form = ItemForm()
    if form.validate_on_submit():
        filename = None
        if 'image' in request.files:
            f = request.files['image']
            if f and allowed_file(f.filename):
                fname = secure_filename(f.filename)
                base, ext = os.path.splitext(fname)
                final = f"{base}_{current_user.id}_{os.urandom(4).hex()}{ext}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], final))
                filename = final

        rating = form.rating.data if form.rating.data and 1 <= form.rating.data <= 10 else None

        it = Item(
            title=form.title.data,
            type=form.type.data,
            genre=form.genre.data,
            rating=rating,
            comment=form.comment.data,
            image=filename,
            user_id=current_user.id
        )
        db.session.add(it)
        db.session.commit()
        flash('Elemento agregado correctamente', 'success')
        return redirect(url_for('dashboard'))
    return render_template('add_item.html', form=form)

@app.route('/edit/<int:item_id>', methods=['GET', 'POST'])
@login_required
def edit_item(item_id):
    it = Item.query.get_or_404(item_id)
    if it.user_id != current_user.id and not current_user.is_moderator():
        abort(403)
    form = ItemForm(obj=it)
    if form.validate_on_submit():
        it.title = form.title.data
        it.type = form.type.data
        it.genre = form.genre.data
        it.rating = form.rating.data
        it.comment = form.comment.data
        if 'image' in request.files:
            f = request.files['image']
            if f and allowed_file(f.filename):
                if it.image:
                    try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], it.image))
                    except: pass
                fname = secure_filename(f.filename)
                final = f"{os.path.splitext(fname)[0]}_{current_user.id}_{os.urandom(4).hex()}{os.path.splitext(fname)[1]}"
                f.save(os.path.join(app.config['UPLOAD_FOLDER'], final))
                it.image = final
        db.session.commit()
        flash('Actualizado correctamente', 'success')
        return redirect(url_for('dashboard'))
    return render_template('edit_item.html', form=form, item=it)

@app.route('/delete/<int:item_id>', methods=['POST'])
@login_required
def delete_item(item_id):
    it = Item.query.get_or_404(item_id)
    if it.user_id != current_user.id and not current_user.is_moderator():
        abort(403)
    if it.image:
        try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], it.image))
        except: pass
    db.session.delete(it)
    db.session.commit()
    flash('Elemento eliminado.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/admin/delete-user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('No puedes eliminar tu propio usuario.', 'danger')
        return redirect(url_for('admin_panel'))
    if user.role == 'admin':
        flash('No puedes eliminar otro administrador.', 'danger')
        return redirect(url_for('admin_panel'))
    for it in user.items:
        if it.image:
            try: os.remove(os.path.join(app.config['UPLOAD_FOLDER'], it.image))
            except: pass
        db.session.delete(it)
    db.session.delete(user)
    db.session.commit()
    flash(f'Usuario {user.username} eliminado correctamente.', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin')
@login_required
@moderator_required
def admin_panel():
    users = User.query.all()
    return render_template('admin_panel.html', users=users, total_users=User.query.count(), total_items=Item.query.count())

@app.route('/moderator')
@login_required
@moderator_required
def moderator_panel():
    usuarios_con_items = db.session.query(User).join(Item).group_by(User.id).having(db.func.count(Item.id) >= 2).all()
    return render_template('moderator_panel.html', total_users=User.query.count(), total_items=Item.query.count(), usuarios=usuarios_con_items)

@app.route('/admin/change-role/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def change_role(user_id):
    user = User.query.get_or_404(user_id)
    user.role = request.form.get('role')
    db.session.commit()
    return redirect(url_for('admin_panel'))

@app.route('/moderator/user/<int:user_id>')
@login_required
@moderator_required
def moderator_user_items(user_id):
    user = User.query.get_or_404(user_id)
    items = Item.query.filter_by(user_id=user.id).all()
    return render_template('moderator_user_items.html', user=user, items=items)

@app.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

@app.route('/perfil/<username>')
@login_required
def public_profile(username):
    user = User.query.filter_by(username=username).first_or_404()
    items = Item.query.filter_by(user_id=user.id).all()
    return render_template('public_profile.html', profile_user=user, items=items)

@app.route('/follow/<int:user_id>', methods=['POST'])
@login_required
def follow_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        abort(403)
    if not current_user.is_following(user):
        follow = Follow(follower_id=current_user.id, followed_id=user.id)
        db.session.add(follow)
        db.session.commit()
    return redirect(url_for('public_profile', username=user.username))

@app.route('/unfollow/<int:user_id>', methods=['POST'])
@login_required
def unfollow_user(user_id):
    follow = Follow.query.filter_by(follower_id=current_user.id, followed_id=user_id).first_or_404()
    db.session.delete(follow)
    db.session.commit()
    user = User.query.get(user_id)
    return redirect(url_for('public_profile', username=user.username))

# ---------- LUMI IA ----------

# Agregamos los métodos GET y POST explícitos, y desactivamos el redirect estricto de barras
@app.route('/lumi', methods=['GET', 'POST'], strict_slashes=False)
@login_required
def lumi():
    # Si por algún error del layout o login llega un POST aquí, lo manejamos devolviendo la vista
    return render_template('lumi.html')

from flask import session, request, jsonify, url_for
from flask_login import login_required, current_user

@app.route("/lumi/chat", methods=["POST"], strict_slashes=False)
@login_required
def lumi_chat():
    data = request.get_json()
    
    if not data:
        return {"respuesta": "No se recibieron datos correctamente."}, 400
        
    mensaje = data.get("mensaje", "").strip()
    mensaje_lower = mensaje.lower()

    # Mapeo dinámico de tipos para optimizar respuestas, condicionales y emojis
    tipos_config = {
        "pelicula": {"emoji": "🎬", "articulo": "la película"},
        "serie":    {"emoji": "📺", "articulo": "la serie"},
        "libro":    {"emoji": "📘", "articulo": "el libro"}
    }

    # ===================================================
    # 1. FLUJO INTERACTIVO MULTI-CONTENIDO (POR PASOS)
    # ===================================================
    estado_actual = session.get('lumi_estado')

    if estado_actual == 'esperando_nombre':
        session['nuevo_titulo'] = mensaje
        session['lumi_estado'] = 'esperando_nota'
        
        tipo_actual = session.get('nuevo_tipo', 'pelicula')
        emoji = tipos_config.get(tipo_actual, {}).get("emoji", "🆕")
        
        return {
            "respuesta": f"Perfecto, anotado: **{mensaje}** {emoji}. ¿Qué calificación le das (del 1 al 10)? ⭐"
        }

    elif estado_actual == 'esperando_nota':
        # Validación optimizada del rango numérico
        if not mensaje.isdigit() or not (1 <= int(mensaje) <= 10):
            return {
                "respuesta": "Por favor, introduce un número entero válido del 1 al 10. ¿Qué nota le pones? ⭐"
            }
        session['nueva_nota'] = int(mensaje)
        session['lumi_estado'] = 'esperando_comentario'
        return {
            "respuesta": "¡Entendido! Por último, déjame un comentario o reseña corta para guardarlo: ✍️"
        }

    elif estado_actual == 'esperando_comentario':
        titulo = session.get('nuevo_titulo')
        nota = session.get('nueva_nota')
        tipo = session.get('nuevo_tipo', 'pelicula')
        comentario_usuario = mensaje 

        try:
            nuevo_item = Item(
                title=titulo,
                type=tipo,
                genre="Sin especificar",
                rating=float(nota),
                comment=comentario_usuario,  # Guardado seguro evitando fallos de None en renderizado
                image=None,
                user_id=current_user.id
            )
            db.session.add(nuevo_item)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return {"respuesta": f"Hubo un problema al guardar en la base de datos: {str(e)}"}, 500
        
        # Limpieza estructurada de la sesión de Flask
        session.pop('lumi_estado', None)
        session.pop('nuevo_titulo', None)
        session.pop('nuevo_tipo', None)
        session.pop('nueva_nota', None)

        articulo_tipo = tipos_config.get(tipo, {}).get("articulo", "el contenido")
        return {
            "respuesta": f"¡Perfecto! Ya agregué {articulo_tipo} correctamente a tu colección con tu comentario. Actualizando tu Dashboard... 🚀",
            "accion": "recargar"
        }

    # ===================================================
    # 2. COMANDOS DIRECTOS DE CONTROL (ACCIONES)
    # ===================================================
    
    # Interceptores optimizados para el inicio del flujo conversacional
    if any(x in mensaje_lower for x in ["agrega una película", "agrega una pelicula", "añadir pelicula", "agregar pelicula"]):
        session['lumi_estado'] = 'esperando_nombre'
        session['nuevo_tipo'] = 'pelicula'
        return {"respuesta": "Claro. ¿Cuál es el nombre de la película? 🎬"}

    if any(x in mensaje_lower for x in ["agrega una serie", "añadir serie", "agregar serie"]):
        session['lumi_estado'] = 'esperando_nombre'
        session['nuevo_tipo'] = 'serie'
        return {"respuesta": "¡Buenísimo! ¿Cuál es el nombre de la serie? 📺"}

    if any(x in mensaje_lower for x in ["agrega un libro", "añadir libro", "agregar libro"]):
        session['lumi_estado'] = 'esperando_nombre'
        session['nuevo_tipo'] = 'libro'
        return {"respuesta": "Excelente elección. ¿Cuál es el título del libro? 📘"}

    # Redirección dinámica al Perfil
    if any(x in mensaje_lower for x in ["muéstrame mi perfil", "muestrame mi perfil", "ir al perfil"]):
        return {
            "respuesta": "Claro, redirigiéndote a tu perfil de inmediato... 👤",
            "accion": "redirigir",
            "url": url_for('perfil')  # Cambia 'perfil' por tu endpoint exacto si difiere
        }

    # Eliminación directa y segura del último registro
    if "elimina mi último" in mensaje_lower or "elimina mi ultimo" in mensaje_lower:
        ultimo = Item.query.filter_by(user_id=current_user.id).order_by(Item.id.desc()).first()
        if ultimo:
            titulo_eliminado = ultimo.title
            db.session.delete(ultimo)
            db.session.commit()
            return {
                "respuesta": f"💥 Hecho. Eliminé tu último contenido registrado: **{titulo_eliminado}**. Actualizando vista...",
                "accion": "recargar"
            }
        return {"respuesta": "No encontré ningún contenido en tu lista para poder eliminar."}

    # ===================================================
    # 3. RESPUESTAS DE CONSULTA (FASE 1 Y FASE 2)
    # ===================================================
    respuesta = responder_lumi(mensaje)

    return {
        "respuesta": respuesta
    }

@app.errorhandler(403)
def forbidden(error): return render_template('403.html'), 403

def create_app_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='admin').first():
            admin = User(username='admin', role='admin'); admin.set_password('admin123')
            db.session.add(admin); db.session.commit()


if __name__ == '__main__':
    create_app_db()
    app.run(host='0.0.0.0', port=5000, debug=True)