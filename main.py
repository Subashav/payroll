import datetime
import logging
import os
import calendar
import pandas as pd
from fpdf import FPDF
from fastapi.responses import FileResponse, StreamingResponse
import io
from contextlib import asynccontextmanager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Models ---
class Employee(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    monthly_salary: float

class PayrollRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    employee_id: int
    employee_name: str
    month_year: str # e.g., "April 2026"
    total_days: int
    worked_days: int
    basic: float
    hra: float
    special: float
    transport: float
    medical: float
    net_salary: float
    created_at: datetime.datetime = Field(default_factory=datetime.datetime.utcnow)

# --- Schemas ---
class PayrollCalculateRequest(BaseModel):
    employee_id: int
    total_days: int = 30
    worked_days: int
    month_year: str

class AttendanceEntry(BaseModel):
    employee_id: int
    worked_days: int

class BulkPayrollRequest(BaseModel):
    month_year: str # e.g. "2026-04"
    attendance: Optional[List[AttendanceEntry]] = None

# --- Database Setup ---
# Database Configuration
# Support for SQLite (default), MySQL/MariaDB, and PostgreSQL
DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    DATABASE_URL = "sqlite:///./payroll.db"
    logger.info("Using local SQLite database")
elif DATABASE_URL.startswith("postgres://"):
    # Fix for Heroku/Vercel Postgres URL naming
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    logger.info("Using PostgreSQL database")
elif DATABASE_URL.startswith("mysql"):
    logger.info("Using MySQL database")

# Standard connection pooling for external DBs
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)



def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    logger.info("Initializing database...")
    create_db_and_tables()
    yield
    # Shutdown logic
    logger.info("Shutting down...")

def get_session():
    with Session(engine) as session:
        yield session

# --- FastAPI App ---
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Monarch Payroll System", lifespan=lifespan)

# Serving static files (like logo.png)
if os.path.exists("public"):
    app.mount("/public", StaticFiles(directory="public"), name="public")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    # Use absolute path for Vercel stability
    path = os.path.join(os.path.dirname(__file__), "index.html")
    return FileResponse(path)

# --- Routes: Employees ---

@app.post("/employees/", response_model=Employee)
def create_employee(employee: Employee, session: Session = Depends(get_session)):
    logger.info(f"Adding employee: {employee.name}")
    session.add(employee)
    session.commit()
    session.refresh(employee)
    return employee

@app.get("/employees/", response_model=List[Employee])
def read_employees(session: Session = Depends(get_session)):
    return session.exec(select(Employee)).all()

@app.put("/employees/{employee_id}", response_model=Employee)
def update_employee(employee_id: int, employee_data: Employee, session: Session = Depends(get_session)):
    db_employee = session.get(Employee, employee_id)
    if not db_employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    db_employee.name = employee_data.name
    db_employee.monthly_salary = employee_data.monthly_salary
    session.add(db_employee)
    session.commit()
    session.refresh(db_employee)
    return db_employee

@app.delete("/employees/{employee_id}")
def delete_employee(employee_id: int, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    session.delete(employee)
    session.commit()
    return {"ok": True}

# --- Routes: Payroll ---

@app.post("/payroll/calculate", response_model=PayrollRecord)
def calculate_payroll(req: PayrollCalculateRequest, session: Session = Depends(get_session)):
    employee = session.get(Employee, req.employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Logic provided by user
    payable_ratio = req.worked_days / req.total_days
    
    basic = employee.monthly_salary * 0.40 * payable_ratio
    hra = employee.monthly_salary * 0.20 * payable_ratio
    special = employee.monthly_salary * 0.25 * payable_ratio
    transport = employee.monthly_salary * 0.10 * payable_ratio
    medical = employee.monthly_salary * 0.05 * payable_ratio
    net_salary = employee.monthly_salary * payable_ratio
    
    record = PayrollRecord(
        employee_id=employee.id,
        employee_name=employee.name,
        month_year=req.month_year,
        total_days=req.total_days,
        worked_days=req.worked_days,
        basic=round(basic, 2),
        hra=round(hra, 2),
        special=round(special, 2),
        transport=round(transport, 2),
        medical=round(medical, 2),
        net_salary=round(net_salary, 2)
    )
    
    session.add(record)
    session.commit()
    session.refresh(record)
    return record

@app.get("/payroll/history", response_model=List[PayrollRecord])
def get_payroll_history(session: Session = Depends(get_session)):
    return session.exec(select(PayrollRecord).order_by(PayrollRecord.created_at.desc())).all()

@app.get("/payroll/days-in-month/{month_year}")
def get_days_in_month(month_year: str):
    try:
        # Expected format: "2026-04"
        year, month = map(int, month_year.split("-"))
        days = calendar.monthrange(year, month)[1]
        return {"days": days}
    except Exception as e:
        logger.error(f"Error calculating days: {e}")
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM")

@app.post("/payroll/calculate-bulk")
def calculate_bulk_payroll(req: BulkPayrollRequest, session: Session = Depends(get_session)):
    try:
        employees = session.exec(select(Employee)).all()
        if not employees:
            raise HTTPException(status_code=404, detail="No employees found")
            
        year, month = map(int, req.month_year.split("-"))
        total_days = calendar.monthrange(year, month)[1]
        month_name = f"{calendar.month_name[month]} {year}"
        
        # Create a mapping for quick lookup if attendance provided
        attendance_map = {a.employee_id: a.worked_days for a in req.attendance} if req.attendance else {}
        
        processed_count = 0
        for emp in employees:
            # Check if record already exists for this employee and month
            existing = session.exec(select(PayrollRecord).where(
                PayrollRecord.employee_id == emp.id, 
                PayrollRecord.month_year == month_name
            )).first()
            
            # If payroll exists, update it instead of skipping (flexible for corrections)
            worked_days = attendance_map.get(emp.id, total_days)
            payable_ratio = worked_days / total_days
            
            if existing:
                existing.worked_days = worked_days
                existing.total_days = total_days
                existing.basic = round(emp.monthly_salary * 0.40 * payable_ratio, 2)
                existing.hra = round(emp.monthly_salary * 0.20 * payable_ratio, 2)
                existing.special = round(emp.monthly_salary * 0.25 * payable_ratio, 2)
                existing.transport = round(emp.monthly_salary * 0.10 * payable_ratio, 2)
                existing.medical = round(emp.monthly_salary * 0.05 * payable_ratio, 2)
                existing.net_salary = round(emp.monthly_salary * payable_ratio, 2)
                session.add(existing)
            else:
                record = PayrollRecord(
                    employee_id=emp.id,
                    employee_name=emp.name,
                    month_year=month_name,
                    total_days=total_days,
                    worked_days=worked_days,
                    basic=round(emp.monthly_salary * 0.40 * payable_ratio, 2),
                    hra=round(emp.monthly_salary * 0.20 * payable_ratio, 2),
                    special=round(emp.monthly_salary * 0.25 * payable_ratio, 2),
                    transport=round(emp.monthly_salary * 0.10 * payable_ratio, 2),
                    medical=round(emp.monthly_salary * 0.05 * payable_ratio, 2),
                    net_salary=round(emp.monthly_salary * payable_ratio, 2)
                )
                session.add(record)
            
            processed_count += 1
        
        session.commit()
        return {"processed": processed_count, "month": month_name}
    except Exception as e:
        logger.error(f"Bulk calculation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/payroll/export/excel")
def export_payroll_excel(session: Session = Depends(get_session)):
    records = session.exec(select(PayrollRecord)).all()
    if not records:
        raise HTTPException(status_code=404, detail="No payroll records found")
    
    data = []
    for r in records:
        d = r.dict()
        d.pop('id')
        d.pop('created_at')
        data.append(d)
        
    df = pd.DataFrame(data)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Payroll History')
    
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=payroll_history.xlsx"}
    )

@app.get("/payroll/export/pdf/{record_id}")
def export_payroll_pdf(record_id: int, session: Session = Depends(get_session)):
    record = session.get(PayrollRecord, record_id)
    if not record:
        raise HTTPException(status_code=404, detail="Record not found")
    
    pdf = FPDF()
    pdf.add_page()
    
    # Background
    pdf.set_fill_color(10, 10, 10)
    pdf.rect(0, 0, 210, 297, 'F')
    
    # Header
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Arial", 'B', 24)
    pdf.cell(0, 20, "MONARCH", ln=True, align='L')
    pdf.set_font("Arial", '', 10)
    pdf.cell(0, 5, "Payroll Management System | Confidential", ln=True, align='L')
    pdf.ln(20)
    
    # Pay Slip Title
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, f"PAY SLIP - {record.month_year.upper()}", ln=True, align='C')
    pdf.ln(10)
    
    # Employee Details
    pdf.set_font("Arial", 'B', 12)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(100, 10, "EMPLOYEE NAME", ln=False)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, record.employee_name, ln=True)
    
    pdf.set_text_color(150, 150, 150)
    pdf.cell(100, 10, "EMPLOYEE ID", ln=False)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, f"MEM-{record.employee_id:04d}", ln=True)
    
    pdf.set_text_color(150, 150, 150)
    pdf.cell(100, 10, "ATTENDANCE", ln=False)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 10, f"{record.worked_days} / {record.total_days} Days", ln=True)
    pdf.ln(15)
    
    # Earnings Table
    pdf.set_fill_color(30, 30, 30)
    pdf.set_draw_color(50, 50, 50)
    pdf.set_font("Arial", 'B', 11)
    pdf.cell(130, 12, "DESCRIPTION", 1, 0, 'L', True)
    pdf.cell(60, 12, "AMOUNT", 1, 1, 'R', True)
    
    pdf.set_font("Arial", '', 11)
    components = [
        ("Basic Salary (40%)", record.basic),
        ("House Rent Allowance (20%)", record.hra),
        ("Special Allowance (25%)", record.special),
        ("Transport Allowance (10%)", record.transport),
        ("Medical Allowance (5%)", record.medical)
    ]
    
    for label, amount in components:
        pdf.cell(130, 10, label, 1)
        pdf.cell(60, 10, f"$ {amount:,.2f}", 1, 1, 'R')
        
    pdf.set_font("Arial", 'B', 12)
    pdf.set_fill_color(40, 40, 40)
    pdf.cell(130, 12, "NET PAYABLE AMOUNT", 1, 0, 'L', True)
    pdf.set_text_color(100, 255, 100)
    pdf.cell(60, 12, f"$ {record.net_salary:,.2f}", 1, 1, 'R', True)
    
    pdf.ln(30)
    pdf.set_text_color(150, 150, 150)
    pdf.set_font("Arial", 'I', 8)
    pdf.cell(0, 10, "This is a computer generated document and does not require a signature.", ln=True, align='C')
    
    try:
        pdf_output = pdf.output(dest='S').encode('latin-1', 'replace')
    except Exception as e:
        logger.error(f"PDF Output error: {e}")
        pdf_output = pdf.output(dest='S').encode('utf-8', 'ignore')

    return StreamingResponse(
        io.BytesIO(pdf_output),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=payslip_{record.employee_name}_{record.month_year}.pdf"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
