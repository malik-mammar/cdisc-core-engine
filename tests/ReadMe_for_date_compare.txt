Pour que le date_comparator_fonctionnel puisse marcher il faut : 
1- Dans le dataset que l'on veut tester.
    - Rajouter la variable ENDPRO (date de fin du projet) comme nouvelle colonne et 
      la remplir avec la date de fin du projet.
2- Dans le fichier de configuration (yaml) du test, il faut :
    - Rajouter la variable ENDPRO dans la section "values" de la section "Core".
        Check:
            all:
                - name: DOCDAT
                operator: compare_doc_dates_with_project_end
                target: DOCDAT
                comparator: ENDPRO
                value: ENDPRO


