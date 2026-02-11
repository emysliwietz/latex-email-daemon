;; -*- lexical-binding: t; -*-

(TeX-add-style-hook
 "template"
 (lambda ()
   (TeX-add-to-alist 'LaTeX-provided-class-options
                     '(("scrlttr2" "fontsize=10.5pt" "paper=a4" "parskip=half" "enlargefirstpage=on" "fromalign=right" "fromphone=on" "fromemail=on" "fromrule=aftername" "addrfield=on" "backaddress=on" "subject=beforeopening" "locfield=narrow" "foldmarks=on" "")))
   (TeX-add-to-alist 'LaTeX-provided-package-options
                     '(("fontenc" "T1") ("inputenc" "utf8") ("babel" "ngerman") ("geometry" "bottom=30mm") ("hyperref" "") ("ulem" "")))
   (TeX-run-style-hooks
    "latex2e"
    "scrlttr2"
    "scrlttr210"
    "fontenc"
    "inputenc"
    "babel"
    "geometry"
    "hyperref"
    "ulem"))
 :latex)

