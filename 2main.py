saldo = float(input("Saldo: R$ "))
compra = float(input("Compra: R$ "))

if compra <= saldo:
    saldo = saldo - compra
    print("Compra aprovada!")
    print("Novo salldo: R$", saldo) 
else:
    print(" Saldo insuficiente.")
    

          
   